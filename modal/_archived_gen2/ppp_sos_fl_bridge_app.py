"""PPP × FL Sunbiz entities Pattern B Lance bridge — weekly cron.

Delegates to build_bridge_ppp_sos_fl_entities_lance.py with --apply.
Cadence: Tuesday 15:00 UTC — staggered one day after PPP-CA bridge
(PPP-CA runs Mon 15:00 UTC; FL runs Tue 15:00 UTC).

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + bridge-run ledger.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/ppp_sos_fl_bridge_app.py
"""
from __future__ import annotations

import os
import sys

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-ppp-sos-fl-bridge")

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
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=Cron("0 15 * * 2"),  # Tuesday 15:00 UTC — staggered from PPP-CA (Mon 15:00 UTC)
)
def weekly_refresh() -> None:
    """Build PPP FL borrowers × FL Sunbiz entities bridge and write to Lance."""
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from build_bridge_ppp_sos_fl_entities_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_ppp_sos_fl_entities_lance.py", "--apply"]
    raise SystemExit(main())
