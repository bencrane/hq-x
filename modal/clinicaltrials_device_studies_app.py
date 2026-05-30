"""ClinicalTrials.gov device studies — Modal app.

One scheduled function:

  weekly_refresh — runs Mondays 14:00 UTC (Cron "0 14 * * 1", ~10:00 ET).
    Downloads the AACT daily flat-file export -> R2 ZSTD Parquet, then emits
    the Lance dataset.

Source = AACT (not the CT.gov API).  PR #584 sourced this dataset from the
CT.gov API v2, but the API WAF-blocks Modal's egress IPs (403 across multiple
Modal container IPs), so the weekly cron could never run.  AACT publishes a
daily flat-file export served from DigitalOcean Spaces object storage, which
Modal reaches without issue — switching the c1 fetch to AACT makes this cron
actually work from Modal's own egress.

Delegates to:
  scripts/run_clinicaltrials_device_studies_to_r2.py        — c1, AACT -> R2 ingest
  scripts/run_clinicaltrials_device_studies_lance_emit.py   — c4, R2 -> Lance emit

Container sizing.  The AACT daily zip is ~2.45 GB; c1 streams it to disk,
extracts ~1.3 GB of pipe-delimited table files, then runs a DuckDB transform.
The function therefore needs a large ephemeral disk (50 GiB — zip + extracted
files + DuckDB temp spill, with headroom) and 16 GiB memory.  Modal's default
~few-GB scratch disk is not enough for the zip alone.

Secrets (same pair as cal_eprocure_archived / caltrans_ccop apps):
  dex-db  -> DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   -> R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/clinicaltrials_device_studies_app.py

Manual trigger (first-run / re-run — use --detach for the ~2.45 GB download):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/clinicaltrials_device_studies_app.py::weekly_refresh
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-clinicaltrials-device-studies")

# pip_install_from_pyproject pulls httpx + duckdb + pyarrow + boto3 +
# psycopg2-binary + pylance — every dependency c1 (AACT download + DuckDB
# transform) and c4 (Lance emit) need.  No CT.gov API client to drop; c1's
# only HTTP dependency is httpx, which the AACT download path keeps.
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
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]

# Sized for the AACT pipeline: ~2.45 GB zip download + ~1.3 GB extracted
# pipe-delimited table files + DuckDB transform spill.  (CA SoS / FL Sunbiz
# Volume-King ingests are the sizing precedent.)
#
# ephemeral_disk is pinned to Modal's minimum allowed value, 524288 MiB
# (512 GiB) — Modal rejects any request below 524288 MiB ("Function disk
# request out of bounds ... Must be between 524288 and 3145728 MiB"). 512 GiB
# is far more than the pipeline needs (~4 GB working set) but is the floor; the
# default container scratch is too small for the 2.45 GB zip on its own.
INGEST_MEMORY_MB = 16384            # 16 GiB
INGEST_EPHEMERAL_DISK_MB = 524288   # 512 GiB — Modal's minimum ephemeral_disk
INGEST_TIMEOUT_SECONDS = 60 * 60    # 60 min — 2.45 GB download + transform + Lance emit


def _bridge_database_url() -> None:
    """Bridge DATABASE_URL (from dex-db secret) to DEX_DB_URL_* names."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    ephemeral_disk=INGEST_EPHEMERAL_DISK_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 14 * * 1"),  # Mondays 14:00 UTC (~10:00 ET)
)
def weekly_refresh() -> dict:
    """Weekly CT.gov device-studies ingest + Lance emit (AACT source).

    Downloads the AACT daily flat-file export, transforms it into the
    device-study corpus -> R2 ZSTD Parquet, then emits the Lance dataset
    (overwrite mode — latest snapshot only).
    """
    _bridge_database_url()
    snapshot_date = _today_str()
    run_id = str(uuid.uuid4())

    # /root/scripts is where Modal mounts the local scripts/ dir; /root makes
    # scripts._lib.* imports (lance_commit_lock, entity_name_normalize) resolve.
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_clinicaltrials_device_studies_to_r2 import ingest
    from run_clinicaltrials_device_studies_lance_emit import emit
    import datetime as _dt

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="weekly_refresh",
        run_id=run_id,
    ) as hb:
        hb.set_stage("c1_aact_to_r2", {"snapshot_date": snapshot_date})
        ingest(_dt.date.fromisoformat(snapshot_date))
        hb.set_stage("c4_lance_emit")
        emit()

    return {"status": "ok", "run_id": run_id, "snapshot_date": snapshot_date}
