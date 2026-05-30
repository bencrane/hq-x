"""SAM.gov API UEI enrichment — Modal daily cron.

Wraps scripts/run_sam_opps_api_uei_enrichment.py::run_daily. Fires 1h
after sam_opps_active_daily_app so yesterday's new Award Notices have
landed in R2 before enrichment runs. Yesterday's window typically yields
~200 records (1 paginated API call), well under the 1,000/day rate cap.

Schedule: 13:00 UTC daily (1h after the 12:00 UTC active feed cron).

Secrets required (Modal):
    dex-db   — DATABASE_URL pooled to data-engine-x
    bulk-ingest-r2    — R2_*
    sam-api-key       — SAM_API_KEY (one-time setup; see deploy below)

Modal secret setup (run once from hq-all root):

    doppler run --project hq-all --config prd -- bash -c '
        modal secret create --force sam-api-key SAM_API_KEY="$SAM_API_KEY"
    '

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_opps_api_uei_enrichment_app.py

Manual run (e.g., 12-month one-shot backfill):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sam_opps_api_uei_enrichment_app.py::run_backfill_window \\
        --window-from=2025-05-09 --window-to=2026-05-09
"""

from __future__ import annotations

import os
import sys
from datetime import date
from typing import Any

import modal

app = modal.App("data-engine-x-sam-opps-api-uei-enrichment")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("sam-api-key"),
]

# Tiny payload (~200 records/day, 13k for backfill); 2 GB / 30 min plenty.
INGEST_MEMORY_MB = 2048
INGEST_TIMEOUT_SECONDS = 30 * 60


def _bridge_database_url() -> None:
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# apps/hq-x/src/trigger/sam-opps-uei-enrichment-daily.ts POSTs this daily (13:00
# UTC, ~1h after the active-opps feed); spawns run_smart_enrichment async and
# returns the call_id (fire-and-forget — ingest runs in Modal).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_smart_enrichment_via_http() -> dict:
    call = run_smart_enrichment.spawn()
    return {"call_id": call.object_id}


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=4 * 60 * 60,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def run_smart_enrichment() -> dict[str, Any]:
    """Auto-walking daily enrichment.

    Each fire does TWO things:
      1. FORWARD step: enrich any new Award Notices posted since last fire
         (typically yesterday → today, ~200 records, 1 API call).
      2. BACKWARD step: walk history backward in 180-day windows until the
         floor date is reached (default 2016-01-01 = earliest active notice
         in the corpus). ~3-4 API calls per window.

    State persists in bulk_ingest.feed_schedule_config.config JSON
    (`backfill_walk_state` key). Operator never fires manually — full
    history backfill completes autonomously in ~20 days at the SAM_API_KEY
    non-fed tier (10/day cap, ~5 calls/day used per fire).

    After tier upgrade to federal (1000/day): backward step can run
    multiple windows per fire (just bump BACKWARD_STEP_DAYS in the
    script, or fire multiple times).
    """
    _bridge_database_url()
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_sam_opps_api_uei_enrichment import run_smart_walking  # noqa: E402
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_smart_enrichment",
        run_id=run_id,
    ) as hb:
        hb.set_stage("forward_and_backward_walk")
        result = run_smart_walking()
    if isinstance(result, dict):
        result["heartbeat_run_id"] = run_id
    return result


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=4 * 60 * 60,
    memory=INGEST_MEMORY_MB,
)
def run_backfill_window(
    window_from: str, window_to: str,
) -> dict[str, Any]:
    """MANUAL backfill entry point — kept for ops convenience (e.g., to
    re-fire a specific window after a transient outage). Not normally
    called by the operator; the smart walker handles all backfill via
    the cron above."""
    _bridge_database_url()
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_sam_opps_api_uei_enrichment import run_window  # noqa: E402
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_backfill_window",
        run_id=run_id,
    ) as hb:
        hb.set_stage("backfill_window", {"from": window_from, "to": window_to})
        result = run_window(
            posted_from=date.fromisoformat(window_from),
            posted_to=date.fromisoformat(window_to),
        )
    if isinstance(result, dict):
        result["heartbeat_run_id"] = run_id
    return result
