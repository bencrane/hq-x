"""SAM.gov Contract Opportunities — Active feed daily Modal cron.

Wraps scripts/run_sam_opps_ingest.py::run_active in a Modal-scheduled
function. The script handles bulk_ingest.feed_ingest_runs ledger writes
itself; this app's only responsibility is the schedule + the env
bridging.

Schedule: 12:00 UTC daily (~08:00 ET — late enough that SAM's overnight
refresh has settled, early enough to land before business hours).

Secrets required (Modal):
    dex-db   — DATABASE_URL pooled to data-engine-x. The
                        bulk_ingest ledger lives here. Reused across
                        all bulk-ingest Modal apps.
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_opps_active_daily_app.py

Manual run (e.g., backfill a specific snapshot date):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sam_opps_active_daily_app.py::run_active_snapshot \\
        --snapshot-date=2026-05-09
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sam-opps-active-daily")

image = (
    modal.Image.debian_slim(python_version="3.11")
    # SAM.gov's TLS chain has tripped Modal's stale Debian CA bundle in
    # prior ingests; refresh CAs + certifi defensively.
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

# 4 GB / 30min sized for ~80k rows / 227 MB CSV → 30-50 MB Parquet. Well
# under the USAspending bucket; SAM Active is a smaller daily payload.
INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 30 * 60


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the bulk_ingest writers expect
    DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT. Mirror so both readers work."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# apps/hq-x/src/trigger/sam-opps-active-daily.ts POSTs this daily; spawns
# run_active_snapshot async, returns call_id (fire-and-forget — ingest in Modal).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_active_snapshot_via_http(snapshot_date: str | None = None) -> dict:
    call = run_active_snapshot.spawn(snapshot_date=snapshot_date)
    return {"call_id": call.object_id, "snapshot_date": snapshot_date}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def run_active_snapshot(snapshot_date: str | None = None) -> dict[str, Any]:
    """Daily entry point. Defaults to today (UTC) when called by cron;
    accepts an explicit ISO date for backfill."""
    _bridge_database_url()
    sys.path.insert(0, "/root/scripts")
    from run_sam_opps_ingest import run_active  # noqa: E402

    target_date = (
        date.fromisoformat(snapshot_date) if snapshot_date
        else datetime.now(timezone.utc).date()
    )
    return run_active(snapshot_date=target_date)
