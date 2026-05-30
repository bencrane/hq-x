"""USAspending derived views — daily cron (Layer 2 of usaspending-derived-views-daily cycle).

Sequentially runs 4 derived-view emit scripts at 08:30 UTC daily:
  1. winners_recent_lance       (rolling 5-year FPDS+FABS transactions, SAM inline)
  2. awards_by_agency_month_lance   (all-year agency×month rollup)
  3. awards_by_naics_month_lance    (all-year NAICS×month rollup)
  4. awards_by_state_month_lance    (all-year state×month rollup)

Cron schedule: 30 8 * * * (08:30 UTC)
  — 30 min after usaspending_daily_verify_app (08:00 UTC) settles.
  — Avoids collision with usaspending_api_daily_app (06:00 UTC).
  — Override via env: MODAL_USA_DERIVED_VIEWS_CRON

Memory: 32 GB (Phase 3 bump — winners_recent 5y emit: 31M FPDS + 8M FABS + 884K SAM
+ 2.7M POC = ~20-25 GB peak RSS for DuckDB intermediates + Arrow tables.
32 GB is known-safe ceiling per Phase 2 awards_lance 179M-row BTREE build in 32 GB.
Phase 2 was 16 GB for the 90d emit; 5y is ~22x larger so 32 GB gives safe headroom.)
Timeout: 180 min (10800 sec) — 5y emit ~60-120 min wall + 3 rollup emits + retries.

Secrets:
    dex-db   — DEX_DB_URL_DIRECT for lance_commit_lock
    bulk-ingest-r2    — R2 credentials (R2_ENDPOINT + R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY)

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_derived_views_daily_app.py
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

app = modal.App("data-engine-x-usaspending-derived-views-daily")

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

# 32 GB: Phase 3 bump for 5y winners_recent emit (31M FPDS + 8M FABS + 884K SAM +
# 2.7M POC). Peak RSS ~18-25 GB. 32 GB = known-safe ceiling (Phase 2 awards_lance
# 179M-row BTREE was built in 32 GB). Awards rollups (3 emits) peak 4-6 GB each.
# Cycle #3 (subaward parity): subaward leg adds ~1.5-2 GB peak (9.8M row scan,
# 17-col projection, 3rd UNION ALL in 2 CTEs + sub_rows CTAS). Still within 32 GB.
# Override via MODAL_USA_DERIVED_VIEWS_MEMORY_MB env var if needed.
EMIT_MEMORY_MB = int(os.environ.get("MODAL_USA_DERIVED_VIEWS_MEMORY_MB", 32768))
EMIT_TIMEOUT_SECONDS = 180 * 60  # 10800 sec = 180 min (5y emit ~60-120 min wall)

# Emit script paths (mounted under /root/scripts/ inside Modal)
_EMIT_SCRIPTS = [
    "/root/scripts/emit_usaspending_winners_recent_lance.py",
    "/root/scripts/emit_usaspending_awards_by_agency_month_lance.py",
    "/root/scripts/emit_usaspending_awards_by_naics_month_lance.py",
    "/root/scripts/emit_usaspending_awards_by_state_month_lance.py",
]

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Map DATABASE_URL → DEX_DB_URL_DIRECT if the direct URL is not set.

    Modal injects DATABASE_URL from the dex-db secret. The Lance
    commit lock reads DEX_DB_URL_DIRECT (or falls back to DATABASE_URL).
    """
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron(os.environ.get("MODAL_USA_DERIVED_VIEWS_CRON", "30 8 * * *")),
)
def run_derived_views_daily() -> dict[str, Any]:
    """Sequential dispatch of 4 USAspending derived-view emits."""
    _bridge_database_url()
    _ensure_tmpdir()

    started_at = datetime.now(timezone.utc).isoformat()
    t_total = time.time()
    results: list[dict[str, Any]] = []
    overall_status = "succeeded"

    import uuid as _uuid
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    run_id = str(_uuid.uuid4())
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_derived_views_daily",
        run_id=run_id,
    )
    hb_cm.__enter__()

    for script_path in _EMIT_SCRIPTS:
        hb_cm.set_stage(f"emit_{script_path.rsplit('/', 1)[-1]}", {"total": len(_EMIT_SCRIPTS)})
        script_name = script_path.rsplit("/", 1)[-1]
        t0 = time.time()
        logger.info("starting emit: %s", script_name)

        try:
            result = subprocess.run(
                [sys.executable, script_path, "--apply"],
                capture_output=True,
                text=True,
                env=os.environ,
                check=False,
                timeout=EMIT_TIMEOUT_SECONDS - 120,  # leave 2 min of headroom
            )
        except subprocess.TimeoutExpired as e:
            duration_s = round(time.time() - t0, 1)
            logger.error("emit timed out: %s (%.1fs)", script_name, duration_s)
            results.append({
                "script": script_name,
                "status": "failed",
                "error": f"TimeoutExpired after {duration_s}s",
                "duration_s": duration_s,
            })
            overall_status = "failed"
            continue
        except Exception as e:
            duration_s = round(time.time() - t0, 1)
            logger.error("emit exception: %s — %s", script_name, e)
            results.append({
                "script": script_name,
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "duration_s": duration_s,
            })
            overall_status = "failed"
            continue

        duration_s = round(time.time() - t0, 1)
        stdout_tail = result.stdout[-2000:] if result.stdout else ""
        stderr_tail = result.stderr[-2000:] if result.stderr else ""

        if result.returncode != 0:
            logger.error(
                "emit failed (exit=%d) in %.1fs: %s\nstdout tail:\n%s\nstderr tail:\n%s",
                result.returncode, duration_s, script_name, stdout_tail, stderr_tail,
            )
            results.append({
                "script": script_name,
                "status": "failed",
                "exit_code": result.returncode,
                "duration_s": duration_s,
                "stdout_tail": stdout_tail[-500:],
                "stderr_tail": stderr_tail[-500:],
            })
            overall_status = "failed"
            # Continue to next emit even on failure — partial refresh is better
            # than none (subsequent emits do not depend on prior ones).
            continue

        # Extract lance_rows from "OK — metrics: {...}" line if present
        lance_rows = 0
        for line in result.stdout.splitlines():
            if "OK — metrics:" in line:
                try:
                    import ast
                    metrics_str = line.split("metrics:", 1)[1].strip()
                    metrics = ast.literal_eval(metrics_str)
                    lance_rows = metrics.get("lance_rows", 0)
                except Exception:
                    pass

        logger.info("emit OK: %s (rows=%d, %.1fs)", script_name, lance_rows, duration_s)
        results.append({
            "script": script_name,
            "status": "succeeded",
            "lance_rows": lance_rows,
            "duration_s": duration_s,
        })

    hb_cm.__exit__(None, None, None)
    total_dur = round(time.time() - t_total, 1)
    logger.info(
        "derived_views_daily %s in %.1fs (%d/%d emits succeeded)",
        overall_status, total_dur,
        sum(1 for r in results if r["status"] == "succeeded"),
        len(results),
    )

    return {
        "status": overall_status,
        "run_id": run_id,
        "started_at": started_at,
        "total_duration_s": total_dur,
        "results": results,
    }


@app.local_entrypoint()
def main() -> None:
    out = run_derived_views_daily.remote()
    print(json.dumps(out, indent=2, default=str))
