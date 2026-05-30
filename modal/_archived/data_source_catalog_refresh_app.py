"""Data Source Catalog refresh — hourly Modal cron.

Wraps `scripts/refresh_data_source_catalog.py`. Each tick:
  1. Reads ops.data_source_catalog (is_active = TRUE) from Postgres.
  2. For each source: probes R2 inventory + RW source/MV existence + row count.
  3. UPSERTs into ops.data_source_catalog_r2_snapshot.

The Postgres view ops.data_source_catalog_status LEFT JOINs the snapshot table
so the hq-command "Data Ingests" card always renders, even before the first
cron tick has populated the cache (NULL columns surface as "—" on the card).

Schedule: every hour, on the hour, UTC. Wall-clock per tick: 3-5 min.

Secrets:
  - dex-db  — DATABASE_URL → DEX_DB_URL_*
  - bulk-ingest-r2   — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
  - risingwave-prd   — RISINGWAVE_HOST/PORT/USER/PASSWORD/DATABASE

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/data_source_catalog_refresh_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/data_source_catalog_refresh_app.py::refresh_data_source_catalog
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-data-source-catalog-refresh")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install("boto3>=1.34", "psycopg2-binary>=2.9", "certifi>=2024.7.4")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("risingwave-prd"),
]

INGEST_MEMORY_MB = 1024
INGEST_TIMEOUT_SECONDS = 30 * 60
SUBPROCESS_TIMEOUT_SECONDS = 25 * 60


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; mirror to DEX_DB_URL_* so the
    refresh script's connect helper finds a valid URL."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    schedule=modal.Cron("0 * * * *"),  # every hour, on the hour, UTC
)
def refresh_data_source_catalog() -> dict[str, Any]:
    """Single tick: probe R2 + RW for every active source, UPSERT cache."""
    _bridge_database_url()

    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        ["python3", "/root/scripts/refresh_data_source_catalog.py"],
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return {
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
    }
