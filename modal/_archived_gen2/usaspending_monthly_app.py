"""USAspending Monthly Full Refresh — Modal-hosted monthly bulk ingest.

USAspending publishes a Full bulk archive per fiscal year, refreshed by the
15th of each month at:

    https://files.usaspending.gov/award_data_archive/FY{YYYY}_All_Contracts_Full_{YYYYMMDD}.zip

We fetch the current-FY and prior-FY archives each month and land them in R2
under the dated-snapshot convention enforced by run_usaspending_csv_to_r2_parquet.py:

    s3://dex-raw-landing-zone/usaspending/contracts/year={YYYY}/snapshot={YYYY-MM-DD}/data.parquet

Each (year, snapshot) tuple is a unique R2 key; refreshes never overwrite
history. DuckDB-on-R2 reads time-travel via
read_parquet('s3://.../year=*/snapshot=*/data.parquet', hive_partitioning=1).

Replaces: the previous usaspending_daily_app.py whose daily cadence didn't
match USAspending's monthly publication and wrote 0-byte orphans to
`usaspending/contracts/year=*/month=*/day=*/*.parquet.zst`.

Schedule: 16th of each month at 06:00 UTC. USAspending typically publishes
by the 15th; a 24h grace gives them time to finish if they slip.

Secrets required (Modal):
    dex-db   — DEX_DB_URL_POOLED for ingest-run ledger writes (shared
                        with FMCSA pipeline; ledger is shared across sources).
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        modal deploy modal/usaspending_monthly_app.py

Manual one-off run (e.g. backfill a specific FY):
    cd ~/hq-all/apps/data-engine-x && \\
        modal run modal/usaspending_monthly_app.py::run_monthly_refresh \\
        --target-fys=2025,2026
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-monthly-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4", "requests>=2.32.0")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Memory sized for the FY ingest's pyarrow streaming + ParquetWriter state.
# An FY of contract transactions (~6.7M rows × ~300 cols of text) peaks at
# ~6-8 GB in pyarrow batch state with the 64MB CSV block size we use.
INGEST_MEMORY_MB = 16384
# Cap total cron tick at 8h: a full FY ingest is ~10 min wall-clock; 8h is
# a fat ceiling that prevents runaway in pathological cases.
INGEST_TIMEOUT_SECONDS = 8 * 60 * 60

ARCHIVE_BASE_URL = "https://files.usaspending.gov/award_data_archive"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; downstream code expects DEX_DB_URL_POOLED.
    Mirror it across so both readers work."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _current_fiscal_years() -> list[int]:
    """Federal FY runs Oct-Sep. Refresh the current FY and the prior FY each
    month — prior FY gets late corrections, current FY gets new transactions.

    For a calendar month inside Oct-Dec → current_FY = next_calendar_year.
    For Jan-Sep → current_FY = current_calendar_year.
    """
    today = datetime.now(timezone.utc).date()
    if today.month >= 10:
        current_fy = today.year + 1
    else:
        current_fy = today.year
    return [current_fy - 1, current_fy]


def _find_latest_zip_for_fy(fy: int, session_today: date | None = None) -> tuple[str, str] | None:
    """HEAD-probe USAspending Award Archive for the latest published Full ZIP
    for the given FY. Returns (filename, url) or None if not found.

    USAspending publishes by the 15th of each month. We probe the latest
    likely publication dates first (current month's 15th, 20th, 10th, 5th,
    1st, then prior month's tail) until we find a 200 OK.
    """
    import requests

    if session_today is None:
        session_today = datetime.now(timezone.utc).date()

    # Build candidate dates: current month + prior month, scan plausible
    # publication days. Most recent first.
    candidates: list[date] = []
    for month_offset in (0, -1, -2):
        # Derive year+month
        year, month = session_today.year, session_today.month + month_offset
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        # Publication-day candidates (descending — newest first).
        for d in (20, 18, 16, 15, 14, 12, 10, 8, 5, 3, 1):
            try:
                candidates.append(date(year, month, d))
            except ValueError:
                continue

    for cand in candidates:
        if cand > session_today:
            continue
        ymd = cand.strftime("%Y%m%d")
        filename = f"FY{fy}_All_Contracts_Full_{ymd}.zip"
        url = f"{ARCHIVE_BASE_URL}/{filename}"
        try:
            resp = requests.head(url, timeout=30, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            return filename, url

    return None


def _download(url: str, dest: Path) -> int:
    """Stream-download a URL to a local Path. Returns bytes written."""
    import requests

    bytes_written = 0
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
    return bytes_written


def _derive_snapshot_date(filename: str) -> str:
    """Extract YYYY-MM-DD from FY{YYYY}_All_Contracts_Full_{YYYYMMDD}.zip."""
    import re
    m = re.search(r"_(\d{4})(\d{2})(\d{2})\.zip$", filename)
    if not m:
        raise ValueError(f"Cannot derive snapshot date from filename: {filename}")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# Trigger task apps/hq-x/src/trigger/usaspending-monthly-refresh.ts POSTs this
# monthly; we spawn run_monthly_refresh async (current FYs) and return the
# call_id immediately (fire-and-forget — ingest runs in Modal).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_monthly_refresh_via_http() -> dict:
    call = run_monthly_refresh.spawn()
    return {"call_id": call.object_id}


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def run_monthly_refresh(
    target_fys: str | None = None,
    snapshot_date_override: str | None = None,
) -> dict[str, Any]:
    """Download + ingest the latest Full archive for each target FY.

    target_fys: comma-separated FY numbers (e.g. "2025,2026") for backfill
        / smoke. Defaults to current FY + prior FY.
    snapshot_date_override: YYYY-MM-DD to force-override the snapshot date
        derived from the ZIP filename. Useful when manually backfilling
        a known archive.
    """
    _bridge_database_url()

    if target_fys:
        fys = [int(x.strip()) for x in target_fys.split(",") if x.strip()]
    else:
        fys = _current_fiscal_years()

    results: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc).isoformat()

    import uuid as _uuid
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    run_id = str(_uuid.uuid4())
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_monthly_refresh",
        run_id=run_id,
    )
    hb_cm.__enter__()

    for fy in fys:
        hb_cm.set_stage(f"fy_{fy}", {"target_fys": fys})
        t0 = time.monotonic()
        result: dict[str, Any] = {"fy": fy, "started_at_utc": datetime.now(timezone.utc).isoformat()}

        # Discover latest URL
        found = _find_latest_zip_for_fy(fy)
        if not found:
            result["status"] = "not_found"
            result["elapsed_s"] = time.monotonic() - t0
            results.append(result)
            print(f"[FY{fy}] no archive found in HEAD-probe window", flush=True)
            continue
        filename, url = found
        result["filename"] = filename
        result["url"] = url
        snapshot_date = snapshot_date_override or _derive_snapshot_date(filename)
        result["snapshot_date"] = snapshot_date
        print(f"[FY{fy}] found {filename}  url={url}  snapshot={snapshot_date}", flush=True)

        # Download
        with tempfile.NamedTemporaryFile(
            prefix=f"usa_fy{fy}_", suffix=".zip", delete=False
        ) as tf:
            zip_path = Path(tf.name)
        try:
            bytes_downloaded = _download(url, zip_path)
            result["bytes_downloaded"] = bytes_downloaded
            print(f"[FY{fy}] downloaded {bytes_downloaded:,} bytes -> {zip_path}", flush=True)

            # Invoke the ingest script as a subprocess so we get its full
            # streaming-progress logging in Modal's stdout.
            cmd = [
                sys.executable,
                "/root/scripts/run_usaspending_csv_to_r2_parquet.py",
                "--zip", str(zip_path),
                "--year", str(fy),
                "--snapshot-date", snapshot_date,
            ]
            proc = subprocess.run(cmd, capture_output=False, check=False)
            result["ingest_exit_code"] = proc.returncode
            result["status"] = "complete" if proc.returncode == 0 else "ingest_failed"
        finally:
            if zip_path.exists():
                zip_path.unlink()

        result["elapsed_s"] = time.monotonic() - t0
        results.append(result)

    hb_cm.__exit__(None, None, None)
    return {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "target_fys": fys,
        "results": results,
    }
