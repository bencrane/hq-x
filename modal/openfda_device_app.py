"""openFDA Medical Device (510k + PMA + Classification) — Modal app.

Weekly cron that ingests all 3 openFDA device variants from the bulk manifest
(https://api.fda.gov/download.json), writes ZSTD Parquet to R2, then emits
the 3 Lance datasets at polaris-warehouse/openfda/.

Delegates to:
  scripts/run_openfda_device_to_r2.py     — R2 ingest (all 3 variants)
  scripts/run_openfda_device_lance_emit.py — Lance emit (all 3 variants)

Secrets:
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/openfda_device_app.py

Manual trigger (first-run / re-run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/openfda_device_app.py::weekly_ingest_and_emit
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-openfda-device")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "certifi>=2024.7.4",
        "pandas>=2.0",
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


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 14 * * 1"),  # Mondays 14:00 UTC weekly
)
def weekly_ingest_and_emit() -> dict:
    """Weekly openFDA device ingest + Lance emit.

    1. Downloads all 3 variants from the openFDA bulk manifest to R2.
       Skip-if-unchanged on export_date per variant.
    2. Emits 3 Lance datasets at polaris-warehouse/openfda/.
    """
    _bridge_database_url()
    snapshot_date_str = _today_str()

    # /root/scripts is where Modal mounts the local scripts/ dir.
    # Insert /root so that scripts._lib.* imports resolve correctly.
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")

    import datetime as _dt
    from run_openfda_device_to_r2 import ingest
    from run_openfda_device_lance_emit import emit

    snapshot_date = _dt.date.fromisoformat(snapshot_date_str)

    # Step 1: R2 ingest (all 3 variants)
    ingest(variants=["510k", "pma", "classification"], snapshot_date=snapshot_date)

    # Step 2: Lance emit (all 3 variants)
    emit()

    return {
        "status": "ok",
        "snapshot_date": snapshot_date_str,
        "variants": ["510k", "pma", "classification"],
    }
