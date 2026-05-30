"""FMCSA carrier_essentials → embeddings Lance dataset (Phase 4 cron).

Phase 4 of the multi-phase hq-all rebuild — vector layer activation. Daily
cron that:

  1. Reads the FMCSA carrier_essentials Lance dataset (produced by the
     Wave 3 canary cron at 06:30 UTC).
  2. Composes a profile-text per carrier (eligibility: active, with at
     least one power unit).
  3. Embeds new/changed profiles via the configured provider — default
     OpenAI ``text-embedding-3-small`` (1536-dim), fallback
     ``sentence-transformers/all-MiniLM-L6-v2`` (384-dim, free).
  4. Writes the embeddings Lance dataset at
     ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_embeddings_lance/``
     with primary-key BTREE + IVF_PQ vector index (cosine metric).
  5. Optimizes (compact + cleanup_older_than=7d).
  6. Records the run in ``ops.data_source_ingest_runs`` for system-health
     observability against the registered 24h SLA.

Schedule: ``45 7 * * *`` UTC. Offset 1h15min after the carrier_essentials
Lance canary cron (06:30 UTC) so the source Lance dataset is settled.

Cost (OpenAI): ~$6 for the initial backfill (1.95M carriers × ~150 tokens
× $0.02/1M). Daily incremental: $0.05-0.50.
Cost (sentence-transformers): $0 in API; ~$0.20-0.50 in Modal compute.

Switching providers
-------------------
Default is OpenAI. To swap to sentence-transformers (e.g. while OpenAI
quota is unavailable), set ``EMBEDDING_PROVIDER=sentence-transformers``
in the modal secret ``openai-api-key`` (the value can be any string when
the provider is sentence-transformers — see ``EMBEDDING_PROVIDER`` env in
``scripts/_lib/embedding_emit.py``).

Note: changing providers means changing the embedding dimension, which
invalidates the existing IVF_PQ index. The next emit run will rebuild
the index but the dataset will carry mixed model_versions until all rows
are re-embedded. The vector_query layer dispatches by ``model_version``
column so cross-model reads are detected, but the SAFE path is:

  1. Truncate the existing embeddings dataset (manual op).
  2. Flip ``EMBEDDING_PROVIDER``.
  3. Re-run the cron for full re-embed.

Secrets required (Modal):
    dex-db                — DEX_DB_URL_DIRECT for commit lock +
                                     ops.data_source_ingest_runs writes.
    bulk-ingest-r2                 — R2_ENDPOINT / R2_ACCESS_KEY_ID /
                                     R2_SECRET_ACCESS_KEY.
    openai-api-key                 — OPENAI_API_KEY (only used when
                                     EMBEDDING_PROVIDER=openai).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_carrier_essentials_embedding_emit_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/fmcsa_carrier_essentials_embedding_emit_app.py::emit
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-fmcsa-carrier-essentials-embedding-emit")

# Embeddings emit is dominated by either network I/O (OpenAI) or CPU
# (sentence-transformers). 4 GiB memory comfortably handles the diff
# staging + carry-over rows for both. 4h timeout allows the OpenAI path's
# full backfill (~2h wall time for 2M rows at 256/batch and ~3000ms/batch).
# sentence-transformers does the 2M backfill in ~1h on CPU.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "openai>=1.30,<2.0",
        "sentence-transformers>=2.7,<4.0",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# EMBEDDING_PROVIDER defaults to 'openai'; switch to 'sentence-transformers'
# via a Modal secret if OpenAI quota is unavailable. The provider name is
# read in scripts/_lib/embedding_emit.py at module-init time, so any
# Modal Secret carrying EMBEDDING_PROVIDER will be picked up.
#
# 2026-05-12 ship-state: openai-api-key exists in Modal but is out of quota
# (verified via openai.embeddings.create returning 429 insufficient_quota).
# Operator can flip the env var when quota is restored. Until then the
# default OpenAI path will fail on first emit; the embeddings dataset's
# initial population was produced via local sentence-transformers
# (apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_embedding_emit.py
# with EMBEDDING_PROVIDER=sentence-transformers).
FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("openai-secret"),
]

EMIT_MEMORY_MB = 4096
EMIT_TIMEOUT_SECONDS = 60 * 60 * 4

DISPLAY_NAME = "fmcsa_carrier_essentials_embeddings_lance"

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the embed script reads
    DEX_DB_URL_DIRECT for the commit lock + ingest-run ledger."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    """Redirect TMPDIR per LanceDB OSS ops discipline."""
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)


def _connect():
    import psycopg
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=True)


def _resolve_source_id() -> str | None:
    """Look up the source_id for the embeddings dataset in ops.data_sources.

    Returns None if not yet seeded — the seed is a separate one-time step;
    this cron tolerates its absence and skips with a warning so the
    schedule doesn't blow up.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
            (DISPLAY_NAME,),
        ).fetchone()
        return str(row[0]) if row else None


def _record_start(source_id: str, metadata: dict[str, Any]) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
                (source_id, started_at, status, run_metadata)
            VALUES (%s, NOW(), 'running', %s)
            RETURNING run_id
            """,
            (source_id, json.dumps(metadata)),
        ).fetchone()
        assert row is not None
        return str(row[0])


def _record_complete(
    run_id: str,
    *,
    status: str,
    rows_ingested: int = 0,
    bytes_written: int = 0,
    error_message: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.data_source_ingest_runs
            SET status        = %s,
                completed_at  = NOW(),
                rows_ingested = %s,
                bytes_written = %s,
                error_message = %s,
                run_metadata  = run_metadata || %s::jsonb
            WHERE run_id = %s
            """,
            (status, rows_ingested, bytes_written, error_message,
             json.dumps(extra_metadata or {}), run_id),
        )


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
    schedule=modal.Cron("45 7 * * *"),  # 07:45 UTC, after fmcsa-carrier-essentials-lance-emit
)
def emit() -> dict[str, Any]:
    """Run the embedding emit script. Records start/complete in
    ops.data_source_ingest_runs. Raises on failure so Modal flags red."""
    _bridge_database_url()
    _ensure_tmpdir()

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    source_id = _resolve_source_id()
    if source_id is None:
        logger.warning(
            "ops.data_sources has no row %r; skipping run "
            "(operator: run scripts/seed_carrier_essentials_embeddings_observability_source.py)",
            DISPLAY_NAME,
        )
        return {"status": "skipped", "reason": "observability seed not applied"}

    metadata = {
        "writer": "fmcsa-carrier-essentials-embedding-emit",
        "started_at": started_at,
    }
    run_id = _record_start(source_id, metadata)
    logger.info("recorded run start: run_id=%s source_id=%s", run_id, source_id)

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit",
        run_id=run_id,
    )
    hb_cm.__enter__()
    hb_cm.set_stage("subprocess_embedding_emit")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "/root/scripts/run_fmcsa_carrier_essentials_embedding_emit.py",
                "--apply",
            ],
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=EMIT_TIMEOUT_SECONDS - 60,
        )
    except Exception as e:
        hb_cm.__exit__(None, None, None)
        _record_complete(
            run_id, status="failed",
            error_message=f"{type(e).__name__}: {e}",
            extra_metadata={"exception_type": type(e).__name__},
        )
        raise

    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-2000:] if result.stdout else ""
    stderr_tail = result.stderr[-2000:] if result.stderr else ""

    if result.returncode != 0:
        logger.error(
            "emit failed (exit=%d) in %.1fs\nstdout tail:\n%s\nstderr tail:\n%s",
            result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        _record_complete(
            run_id, status="failed",
            error_message=f"emit exited {result.returncode}: {stderr_tail[-500:]}",
            extra_metadata={
                "exit_code": result.returncode,
                "duration_s": duration_s,
                "stdout_tail": stdout_tail[-1000:],
            },
        )
        hb_cm.__exit__(None, None, None)
        raise RuntimeError(
            f"FMCSA carrier_essentials embedding emit failed "
            f"(exit={result.returncode})"
        )

    # Parse the metrics line for rows_ingested.
    rows = 0
    parsed_metrics: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        if "DONE — metrics:" in line:
            try:
                metrics_str = line.split("metrics:", 1)[1].strip()
                import ast
                parsed_metrics = ast.literal_eval(metrics_str)
                rows = parsed_metrics.get("total_rows", 0)
            except Exception:
                pass

    _record_complete(
        run_id, status="succeeded",
        rows_ingested=rows,
        extra_metadata={
            "duration_s": duration_s,
            "stdout_tail": stdout_tail[-1000:],
            **parsed_metrics,
        },
    )
    logger.info(
        "emit OK in %.1fs (total_rows=%d, run_id=%s, metrics=%s)",
        duration_s, rows, run_id, parsed_metrics,
    )
    hb_cm.__exit__(None, None, None)
    return {
        "status": "succeeded",
        "duration_s": duration_s,
        "total_rows": rows,
        "run_id": run_id,
        "metrics": parsed_metrics,
    }


@app.local_entrypoint()
def main() -> None:
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
