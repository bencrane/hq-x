"""OPSC School Facility Program Funding — Modal app.

Single scheduled function:

  weekly_opsc_refresh — runs weekly on Monday at 16:00 UTC (Cron "0 16 * * 1",
    ~08:00 PT). Resolves CKAN URL → CSV → R2 Parquet, then emits Lance dataset.
    CKAN resource last_modified ~ monthly cadence; weekly cron catches updates
    with no waste.

Delegates to:
  scripts/run_opsc_school_facility_funding_to_r2.py       — R2 ingest
  scripts/run_opsc_school_facility_funding_lance_emit.py  — Lance emit

Secrets:
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/opsc_school_facility_funding_app.py

Manual trigger (first-run or re-run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/opsc_school_facility_funding_app.py::weekly_opsc_refresh
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-opsc-school-facility-funding")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "certifi>=2024.7.4",
        "pandas>=2.0",  # not in pyproject.toml — needed by run_opsc_school_facility_funding_to_r2.py
    )
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]

INGEST_MEMORY_MB = 2048
INGEST_TIMEOUT_SECONDS = 30 * 60


def _bridge_database_url() -> None:
    """Bridge DATABASE_URL (from dex-db secret) to DEX_DB_URL_* names."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _run_ingest(snapshot_date: str) -> None:
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_opsc_school_facility_funding_to_r2 import ingest
    import datetime as _dt

    ingest(_dt.date.fromisoformat(snapshot_date))


def _run_lance_emit() -> None:
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_opsc_school_facility_funding_lance_emit import emit

    emit()


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 16 * * 1"),  # 16:00 UTC Monday (~08:00 PT)
)
def weekly_opsc_refresh() -> dict:
    """Weekly OPSC SFP funding ingest + Lance emit.

    Resolves CKAN URL, downloads CSV, writes today's snapshot to R2, then
    re-emits the Lance dataset (overwrite mode — always the full latest snapshot).
    """
    _bridge_database_url()
    snapshot_date = _today_str()

    _run_ingest(snapshot_date)
    _run_lance_emit()

    return {"status": "ok", "snapshot_date": snapshot_date}
