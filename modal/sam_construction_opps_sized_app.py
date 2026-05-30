"""SAM open construction opportunities — expected-award-size banding — daily cron.

Delegates to run_sam_construction_opps_sized_emit.py with --apply.
Cadence: daily 16:00 UTC — spine opps_active_lance refreshes daily so the
re-emit keeps response_deadline>now() honest and closed opps drop out.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_construction_opps_sized_app.py
"""
from __future__ import annotations

import os
import sys
import uuid

import modal
from modal import Secret

app = modal.App("data-engine-x-sam-construction-opps-sized")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]


def _bridge_database_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# apps/hq-x/src/trigger/sam-construction-opps-sized-daily.ts POSTs this daily;
# spawns daily_refresh async, returns call_id (fire-and-forget — ingest in Modal).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_construction_refresh_via_http() -> dict:
    call = daily_refresh.spawn()
    return {"call_id": call.object_id}


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 60,
    memory=8192,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def daily_refresh() -> None:
    """Build SAM construction opps expected-award-size band dataset and write to Lance."""
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_sam_construction_opps_sized_emit import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["run_sam_construction_opps_sized_emit.py", "--apply"]
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="daily_refresh",
        run_id=run_id,
    ) as hb:
        hb.set_stage("sized_emit")
        raise SystemExit(main())
