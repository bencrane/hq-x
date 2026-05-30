"""USAspending recipient-features Lance emit — monthly cron.

Pre-aggregates USAspending contract data at recipient_uei grain into a Lance
dataset on R2 carrying the matching features (past-PoP centroid, capacity
envelope, NAICS/PSC mix, agency familiarity, recency windows) consumed by the
EquipmentWork prospect-preview opportunity-relevance scoring against
polaris-warehouse/sam_gov/opps_active_lance.

Schedule: ``0 9 16 * *`` UTC — monthly, 16th @ 09:00 UTC, 2h downstream of
usaspending_contracts_lance_emit at ``0 7 16 * *`` (which completes ~1h after
the monthly bulk drop at 06:00 UTC). The directive's prose 'daily cron' is a
misstatement — the upstream contracts_lance refreshes monthly, so daily runs
would read identical input on 29/30 days. Monthly at 09:00 UTC on the 16th
avoids the cron collision at 07:00 and lands after daily_verify at 08:00.

Existing cron slots in the usaspending namespace:
  0 6 * * *   — api-daily-delta
  0 7 16 * *  — contracts-lance-emit (upstream; fires monthly on 16th)
  0 4 * * *   — recipient-grain-lance-emit (daily)
  0 8 * * *   — daily-verify
  0 9 16 * *  — THIS APP (monthly, 16th, 1h after daily-verify)

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + ledger.
    bulk-ingest-r2     — R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_recipient_features_app.py

First populate (foreground):
    doppler run --project hq-all --config prd -- \\
        modal run --detach modal/usaspending_recipient_features_app.py::main

Monitoring:
    modal app list --json | jq -e '.[] \\
      | select(.Description=="data-engine-x-usaspending-recipient-features") \\
      | select(.State=="deployed")'

Source: directive 2026-05-19-usaspending-recipient-features-lance-emit.md
Precedent: modal/usaspending_recipient_grain_lance_emit_app.py (same pattern).
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

app = modal.App("data-engine-x-usaspending-recipient-features")

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

# 8 GB gives 2x headroom over the recipient-grain precedent (4 GB / ~134K rows)
# for the additional DuckDB GROUP BY pass over 15.5M contracts_lance rows.
EMIT_MEMORY_MB = 8192
EMIT_TIMEOUT_SECONDS = 60 * 60  # 1 hour per directive

DISPLAY_NAME = "usaspending_recipient_features_lance"
SCRIPT_PATH = "/root/scripts/run_usaspending_recipient_features_lance_emit.py"

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.makedirs("/tmp/lance", exist_ok=True)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=8192,  # MB — 2x headroom over recipient-grain (4096) for GROUP BY over 15.5M rows
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron("0 9 16 * *"),  # monthly, 16th @ 09:00 UTC
)
def emit() -> dict[str, Any]:
    """Monthly recipient-features Lance emit (scheduled and manual entry point)."""
    _bridge_database_url()
    _ensure_tmpdir()

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    logger.info("starting %s emit at %s", DISPLAY_NAME, started_at)

    cmd = [sys.executable, SCRIPT_PATH, "--apply"]

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
    hb_cm.set_stage("subprocess_recipient_features_emit")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=EMIT_TIMEOUT_SECONDS - 60,
        )
    except Exception as exc:
        hb_cm.__exit__(None, None, None)
        raise RuntimeError(
            f"{DISPLAY_NAME} Lance emit subprocess failed: {type(exc).__name__}: {exc}"
        ) from exc

    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-3000:] if result.stdout else ""
    stderr_tail = result.stderr[-2000:] if result.stderr else ""

    if result.returncode != 0:
        logger.error(
            "emit failed (exit=%d) in %.1fs\nstdout tail:\n%s\nstderr tail:\n%s",
            result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        hb_cm.__exit__(None, None, None)
        raise RuntimeError(
            f"{DISPLAY_NAME} Lance emit failed (exit={result.returncode}): "
            f"{stderr_tail[-500:]}"
        )

    # Parse rows_emitted from the emit script's stdout line:
    # "OK — metrics: {'rows_emitted': N, ...}"
    rows_emitted = 0
    for line in result.stdout.splitlines():
        if "OK — metrics:" in line:
            try:
                metrics_str = line.split("metrics:", 1)[1].strip()
                import ast
                metrics = ast.literal_eval(metrics_str)
                rows_emitted = metrics.get("rows_emitted", 0)
            except Exception:
                pass

    logger.info(
        "emit OK in %.1fs (rows_emitted=%d)", duration_s, rows_emitted,
    )
    hb_cm.__exit__(None, None, None)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "duration_s": duration_s,
        "rows_emitted": rows_emitted,
        "stdout_tail": stdout_tail[-500:],
    }


@app.local_entrypoint()
def main() -> None:
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
