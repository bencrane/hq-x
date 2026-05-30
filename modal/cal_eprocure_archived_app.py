"""CA Cal eProcure archived opportunities — Modal app.

Two scheduled functions:

  monthly_ncb_refresh — runs on the 5th of each month (Cron "0 7 5 * *").
    Downloads NCB XLSX → R2, then emits Lance dataset.
    NCB is a genuine monthly refresh (~469 rows, Special Category ≥$1M awards).

  historical_po_once — no schedule (manual trigger only).
    One-shot historical backfill for the DGS Purchase Order dataset (FY 2012-2015,
    344K rows, static since 2019). Run once at deploy time; never again unless the
    source is updated by DGS.

Both functions delegate to:
  scripts/run_cal_eprocure_archived_to_r2.py — R2 ingest
  scripts/run_cal_eprocure_archived_lance_emit.py — Lance emit

Secrets (same as SAM opps archived weekly app per directive):
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/cal_eprocure_archived_app.py

Manual trigger (PO one-shot):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/cal_eprocure_archived_app.py::historical_po_once

Manual trigger (NCB — e.g. for first-run or re-run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/cal_eprocure_archived_app.py::monthly_ncb_refresh
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-cal-eprocure-archived")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "certifi>=2024.7.4",
        "pandas>=2.0",    # not in pyproject.toml — needed by run_cal_eprocure_archived_to_r2.py
        "openpyxl",       # required for pandas.read_excel on .xlsx files
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


def _run_ingest(variant: str, snapshot_date: str) -> None:
    # /root/scripts is where Modal mounts the local scripts/ dir.
    # We also insert /root so that scripts._lib.* imports resolve correctly.
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_cal_eprocure_archived_to_r2 import ingest_ncb, ingest_po
    import datetime as _dt

    date = _dt.date.fromisoformat(snapshot_date)
    if variant == "po":
        ingest_po(date)
    elif variant == "ncb":
        ingest_ncb(date)
    else:
        raise ValueError(f"unknown variant: {variant}")


def _run_lance_emit(variant: str) -> None:
    # /root/scripts is where Modal mounts the local scripts/ dir.
    # We also insert /root so that scripts._lib.* imports (lance_commit_lock etc.) resolve.
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_cal_eprocure_archived_lance_emit import emit_ncb, emit_po

    if variant == "po":
        emit_po()
    elif variant == "ncb":
        emit_ncb()
    else:
        raise ValueError(f"unknown variant: {variant}")


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    schedule=Cron("0 7 5 * *"),  # 07:00 UTC on the 5th of each month
)
def monthly_ncb_refresh() -> dict:
    """Monthly NCB ingest + Lance emit. Runs on the 5th of each month.

    Downloads the current XLSX snapshot, writes R2 Parquet, then emits the
    Lance dataset (overwrite mode — always the full latest snapshot).
    """
    _bridge_database_url()
    snapshot_date = _today_str()

    _run_ingest("ncb", snapshot_date)
    _run_lance_emit("ncb")

    return {"status": "ok", "variant": "ncb", "snapshot_date": snapshot_date}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # No schedule — this is a one-shot historical backfill.
    # Run once at deploy via `modal run ... ::historical_po_once`.
    # DGS PO data is static since 2019 (FY 2012-2015 only); no future
    # monthly refresh expected from this CKAN package.
)
def historical_po_once() -> dict:
    """One-shot PO historical backfill + Lance emit.

    Downloads the full DGS Purchase Order CSV (344K rows, FY 2012-2015),
    writes R2 Parquet, then emits the Lance dataset.
    """
    _bridge_database_url()
    snapshot_date = _today_str()

    _run_ingest("po", snapshot_date)
    _run_lance_emit("po")

    return {"status": "ok", "variant": "po", "snapshot_date": snapshot_date}
