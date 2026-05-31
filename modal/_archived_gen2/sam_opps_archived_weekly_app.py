"""SAM.gov Contract Opportunities — Archived (current FY) weekly Modal cron.

Wraps scripts/run_sam_opps_ingest.py::run_archived in a Modal-scheduled
function. Pulls only the CURRENT fiscal year's archived feed weekly;
older FYs are stable and were backfilled once via the script's
--backfill-historical mode.

The current fiscal year is computed at run time from UTC now: federal FY
runs Oct 1 → Sep 30, named after the calendar year it ends in. Oct-Dec
2025 = FY2026; Jan-Sep 2026 = FY2026; Oct 2026 onward = FY2027.

Schedule: 14:00 UTC every Monday.

Secrets: same as sam_opps_active_daily_app.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_opps_archived_weekly_app.py

Manual run (e.g., backfill a specific FY):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sam_opps_archived_weekly_app.py::run_archived_snapshot \\
        --fy=2025
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sam-opps-archived-weekly")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Smaller payload than active (~16-30k rows for current partial FY) →
# 2 GB / 15min is plenty.
INGEST_MEMORY_MB = 2048
INGEST_TIMEOUT_SECONDS = 15 * 60


def _bridge_database_url() -> None:
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _current_federal_fy(today: date | None = None) -> int:
    """Federal FY ends Sep 30, named after the calendar year it ends in."""
    today = today or datetime.now(timezone.utc).date()
    return today.year + 1 if today.month >= 10 else today.year


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# apps/hq-x/src/trigger/sam-opps-archived-weekly.ts POSTs this weekly; spawns
# run_archived_snapshot async (current FY), returns call_id (fire-and-forget).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_archived_snapshot_via_http() -> dict:
    call = run_archived_snapshot.spawn()
    return {"call_id": call.object_id}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def run_archived_snapshot(fy: int | None = None) -> dict[str, Any]:
    """Weekly entry point. Defaults to current FY when called by cron;
    accepts an explicit FY for backfill / manual ops."""
    _bridge_database_url()
    sys.path.insert(0, "/root/scripts")
    from run_sam_opps_ingest import run_archived  # noqa: E402

    target_fy = fy if fy is not None else _current_federal_fy()
    return run_archived(fy=target_fy)
