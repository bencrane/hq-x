"""USAspending × NY SoS Active Corporations Pattern B Lance bridge — weekly cron.

Delegates to build_bridge_usaspending_sos_ny_owner_lance.py with --apply.
Cadence: Monday 15:00 UTC (10:00 ET) — staggered +1h after sam_sos_ny_entities
bridge (Monday 14:00 UTC) to avoid R2/DuckDB contention.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + bridge-run ledger.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_sos_ny_owner_bridge_app.py
"""
from __future__ import annotations

import os
import sys
import uuid

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-usaspending-sos-ny-owner-bridge")

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


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 60,
    memory=8192,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=Cron("0 15 * * 1"),  # Monday 15:00 UTC (10:00 ET) — weekly, +1h after sam_sos_ny
)
def weekly_refresh() -> None:
    """Build USAspending × NY SoS Active Corporations bridge and write to Lance."""
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from build_bridge_usaspending_sos_ny_owner_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_usaspending_sos_ny_owner_lance.py", "--apply"]
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="weekly_refresh",
        run_id=run_id,
    ) as hb:
        hb.set_stage("bridge_build")
        raise SystemExit(main())
