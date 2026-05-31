"""openFDA Medical Device applicants x PDL companies Pattern B Lance bridge — weekly cron.

Delegates to build_bridge_openfda_device_pdl_lance.py with --apply.
Cadence: Monday 16:00 UTC — staggered after the openFDA source refresh and after
the existing bridge crons (SAM-CA at 13:00 UTC, PPP-SoS-CA at 15:00 UTC).

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + bridge-run ledger.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/openfda_device_pdl_bridge_app.py
"""
from __future__ import annotations

import os
import sys

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-openfda-device-pdl-bridge")

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
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=Cron("0 16 * * 1"),  # Monday 16:00 UTC — staggered after openFDA refresh + bridge crons
)
def weekly_refresh() -> None:
    """Build openFDA Medical Device x PDL companies bridge and write to Lance."""
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from build_bridge_openfda_device_pdl_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_openfda_device_pdl_lance.py", "--apply"]
    raise SystemExit(main())
