"""USAspending × FL Sunbiz entities Pattern B Lance bridge — weekly cron.

Delegates to build_bridge_usaspending_sos_fl_owner_lance.py with --apply.
Cadence: Thursday 16:00 UTC — staggered after the Wed crons to avoid R2/DuckDB
contention with other weekly bridge refreshes.

Closes the final USAspending × FL SoS matrix gap (Cycle 6 of 6 — final gap-fill).
After this bridge, every (CA, FL, NY) SoS is bridged to SBA, USAspending, and SAM.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + bridge-run ledger.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_sos_fl_owner_bridge_app.py
"""
from __future__ import annotations

import os
import sys

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-usaspending-sos-fl-owner-bridge")

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
    .add_local_dir("modal/landing", remote_path="/root/modal/landing")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]


def _bridge_database_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=30 * 60,
    memory=8192,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=Cron("0 16 * * 4"),  # Thursday 16:00 UTC — weekly, staggered after Wed crons
)
def weekly_refresh() -> None:
    """Build USAspending × FL Sunbiz entities bridge and write to Lance."""
    sys.path.insert(0, "/root/scripts")

    _bridge_database_url()

    from build_bridge_usaspending_sos_fl_owner_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_usaspending_sos_fl_owner_lance.py", "--apply"]
    raise SystemExit(main())
