"""USPTO Patents (PatentsView granted + pre-grant bulk) — Modal app.

One scheduled function:

  ingest — runs monthly (Cron "0 8 1 * *", 1st of the month 08:00 UTC).
    Downloads the complete USPTO PatentsView bulk patent corpus from the USPTO
    Open Data Portal (data.uspto.gov) and writes ZSTD Parquet snapshots to R2 at
    s3://dex-raw-landing-zone/uspto-patents/{granted,pregrant}/{table}/snapshot=*/.

The USPTO PatentsView products refresh QUARTERLY; the cron runs monthly so a
release is picked up within ~30 days of publication. Most monthly ticks are a
no-op — the per-(product,table) skip-if-unchanged in run_uspto_patents_to_r2
compares the manifest fileLastModifiedDateTime to the last completed ledger row
and skips every unchanged table.

Delegates to:
  scripts/run_uspto_patents_to_r2.py — manifest-driven ingest of all 60
    PatentsView .tsv.zip tables → R2 ZSTD Parquet.

Lance emit is deliberately NOT wired here — it is a deferred follow-on directive
(60 tables is too many to bundle Lance emits into one cycle). This app is an
R2-only ingest.

Container sizing. The two products total ~31 GB zipped; the largest single
table (g_other_reference) is ~22 GB uncompressed TSV. run_uspto_patents_to_r2
processes one table at a time and deletes every local artifact before the next,
so peak disk ≈ (largest .tsv.zip + its TSV + its Parquet) ≈ ~30 GB. ephemeral_disk
is pinned to Modal's minimum allowed value (524288 MiB / 512 GiB — Modal rejects
anything below that); 512 GiB far exceeds the ~30 GB peak working set. The full
first ingest is multi-hour (citation tables dominate); timeout is set generously.

Secrets (same pair as cal_eprocure_archived / openfda_device apps):
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/uspto_patents_app.py

Manual trigger (first-run / re-run — use --detach for the ~31 GB multi-hour run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/uspto_patents_app.py::ingest
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-uspto-patents")

# pip_install_from_pyproject pulls duckdb + pyarrow + boto3 + psycopg2-binary +
# requests — every dependency run_uspto_patents_to_r2 needs (download, DuckDB
# transcode, R2 upload, Postgres ledger).
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

# Sized for the PatentsView bulk pipeline: ~31 GB zipped across 60 tables,
# largest single uncompressed TSV ~22 GB. One table at a time, local artifacts
# deleted before the next → peak disk ~30 GB.
#
# ephemeral_disk is pinned to Modal's minimum allowed value, 524288 MiB
# (512 GiB) — Modal rejects any request below 524288 MiB ("Function disk
# request out of bounds ... Must be between 524288 and 3145728 MiB"). 512 GiB
# far exceeds the ~30 GB peak working set; the directive's "≥64 GB" requirement
# is satisfied with wide headroom.
INGEST_MEMORY_MB = 16384             # 16 GiB
INGEST_EPHEMERAL_DISK_MB = 524288    # 512 GiB — Modal's minimum ephemeral_disk
INGEST_TIMEOUT_SECONDS = 8 * 60 * 60  # 8 h — full first ingest of ~150 GB TSV


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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 8 1 * *"),  # 1st of the month 08:00 UTC — quarterly source
)
def ingest() -> dict:
    """Monthly USPTO PatentsView bulk ingest → R2 ZSTD Parquet.

    Downloads both PatentsView products (PVGPATDIS granted + PVPGPUBDIS
    pre-grant), 60 .tsv.zip tables total, and writes per-table ZSTD Parquet
    snapshots to R2. Skip-if-unchanged on the manifest fileLastModifiedDateTime
    makes most monthly ticks a no-op (the source refreshes quarterly).
    """
    _bridge_database_url()
    snapshot_date_str = _today_str()

    # /root/scripts is where Modal mounts the local scripts/ dir; /root makes
    # scripts._lib.* imports resolve correctly.
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")

    import datetime as _dt
    import uuid as _uuid
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_uspto_patents_to_r2 import ingest as run_uspto_patents_to_r2_ingest

    snapshot_date = _dt.date.fromisoformat(snapshot_date_str)
    run_id = str(_uuid.uuid4())

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="ingest",
        run_id=run_id,
    ) as hb:
        hb.set_stage("uspto_patents_to_r2", {"snapshot_date": snapshot_date_str})
        totals = run_uspto_patents_to_r2_ingest(
            products=["granted", "pregrant"],
            snapshot_date=snapshot_date,
        )

    return {
        "status": "ok",
        "run_id": run_id,
        "snapshot_date": snapshot_date_str,
        "completed": totals["completed"],
        "failed": totals["failed"],
        "skipped": totals["skipped"],
    }
