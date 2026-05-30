"""SAM.gov entities longitudinal v2 → Lance emit (one-shot, no cron).

Thin Modal wrapper around `scripts/emit_sam_entities_longitudinal_v2_lance.py`.
The local-machine run path was getting killed mid-write by the orchestrator
harness; Modal containers survive everything until completion or timeout.

This app has NO ``schedule=`` argument — it is INVOKED MANUALLY via
``modal run --detach`` for the one-shot Lance build only. Operator scope rule:
"no ongoing Modal refreshes." This is a one-shot, not a refresh.

Sized for 10.7M-row × 153-col emit (validator-probed) following the CA SoS /
FL Sunbiz / FMCSA-carrier-essentials precedent of ``memory=32768`` for any
Lance emit that BTREE-indexes >9M rows (DataFusion sort-spill OOM mitigation
per ``LANCE_BYPASS_SPILLING=true`` + 32GB RAM headroom).

Secrets:
    dex-db  — DEX_DB_URL_DIRECT for `lance_commit_lock` advisory lock.
    bulk-ingest-r2   — R2 credentials.

Deploy + run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_entities_longitudinal_v2_emit_app.py

    doppler run --project hq-all --config prd -- \\
        modal run --detach modal/sam_entities_longitudinal_v2_emit_app.py
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

app = modal.App("data-engine-x-sam-entities-longitudinal-v2-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# 10.7M rows × 153 cols → BTREE sort-spill is the OOM risk surface. The CA SoS
# (9.4M rows) precedent in PR #464 set the floor at 32 GB; v2 is comparable.
EMIT_MEMORY_MB = 32768
EMIT_TIMEOUT_SECONDS = 60 * 60  # 60 min — pre-v2 (7.7M rows) ran ~17 min local.

SCRIPT_PATH = "/root/scripts/emit_sam_entities_longitudinal_v2_lance.py"

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)
    # Required by Pattern A for BTREE index builds on >1M rows — without it,
    # DataFusion attempts sort-spill to /tmp under restrictive container limits.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
)
def emit() -> dict[str, Any]:
    _bridge_database_url()
    _ensure_tmpdir()

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    logger.info(
        "starting v2 longitudinal emit at %s (memory=%dMB, timeout=%ds)",
        started_at, EMIT_MEMORY_MB, EMIT_TIMEOUT_SECONDS,
    )

    import uuid as _uuid
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    run_id = str(_uuid.uuid4())
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit",
        run_id=run_id,
    )
    hb_cm.__enter__()
    hb_cm.set_stage("subprocess_longitudinal_v2_emit")

    try:
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--apply"],
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=EMIT_TIMEOUT_SECONDS - 60,
        )
    except subprocess.TimeoutExpired as e:
        logger.error("emit timed out after %s", e.timeout)
        hb_cm.__exit__(None, None, None)
        raise

    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-3000:] if result.stdout else ""
    stderr_tail = result.stderr[-3000:] if result.stderr else ""

    if result.returncode != 0:
        logger.error(
            "emit failed (exit=%d) in %.1fs\nstdout tail:\n%s\nstderr tail:\n%s",
            result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        hb_cm.__exit__(None, None, None)
        raise RuntimeError(
            f"v2 longitudinal Lance emit failed (exit={result.returncode}). "
            f"stderr tail: {stderr_tail[-500:]}"
        )

    rows = 0
    for line in result.stdout.splitlines():
        if "OK — metrics:" in line:
            try:
                metrics_str = line.split("metrics:", 1)[1].strip()
                import ast
                metrics = ast.literal_eval(metrics_str)
                rows = metrics.get("lance_rows", 0)
            except Exception:
                pass

    logger.info(
        "emit OK in %.1fs (rows=%d)\nstdout tail:\n%s",
        duration_s, rows, stdout_tail,
    )

    hb_cm.__exit__(None, None, None)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "duration_s": duration_s,
        "rows_ingested": rows,
        "started_at": started_at,
        "stdout_tail": stdout_tail[-1500:],
    }


@app.local_entrypoint()
def main() -> None:
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
