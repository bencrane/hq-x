"""NOAA AIS ingest pipeline (Modal — daily-CSV → R2 fan-out).

Pulls one daily zipped CSV per Modal worker, stream-parses to ZSTD Parquet,
writes to R2 (dex-raw-landing-zone). Postgres holds metadata only
(ops.ais_pings_ingest_runs) — no bulk COPY into entities.source_ais_pings;
the partitioned table is the column-shape reference, not a runtime sink.

Architecture (intentionally simpler than fmcsa_ingest_app):
    - No schedule heartbeat: this is a backfill, not a daily-arrival feed.
      A future Modal Cron can poll for new NOAA-published days.
    - No feed_catalog: one feed shape (daily zipped CSV); the work-unit is a
      calendar date.
    - Worker function `ingest_one_day` is the unit of parallelism. The
      orchestrator `ingest_date_range` fans out across days via .starmap.

Deploy / run (must run from apps/data-engine-x/ — pip_install_from_pyproject
and add_local_dir resolve relative to the modal CLI cwd):

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/noaa_ais_ingest_app.py

Single-day smoke test (~30-80M rows; ~10-30 GB raw → ~600 MB-1.5 GB Parquet):

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/noaa_ais_ingest_app.py::run --year 2024 --month 1 --day 2

Backfill a year (fans out across all days):

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/noaa_ais_ingest_app.py::run \\
            --year 2024 --run-year true --max-concurrency 12

Secrets (create once via Doppler+Modal):

    doppler run --project hq-all --config prd -- bash -c '
        modal secret create --force noaa-ais-db \\
            DATABASE_URL="$DEX_DB_URL_POOLED"
    '
    # bulk-ingest-r2 already exists if FMCSA's R2 path has been deployed —
    # see modal/landing/r2.py docstring for the create command.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from typing import Any

import modal

# ----------------------------------------------------------------------
# App + image
# ----------------------------------------------------------------------

app = modal.App("data-engine-x-noaa-ais-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("modal/noaa_ais", remote_path="/root/noaa_ais")
    # FMCSA's app omits this mount and gets away with it because its R2
    # branch has not yet been exercised in production. We mount it
    # explicitly so `from landing import R2Landing` resolves on the worker.
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# 2026-05-25: `noaa-ais-db` secret was supposed to carry
# `DATABASE_URL="$DEX_DB_URL_POOLED"` (per the deploy docstring above) but
# never existed in the Modal workspace — the deploy probe in PR #733 caught
# it. Removed; `dex-db` injects DATABASE_URL + DEX_DB_URL_POOLED + DEX_DB_URL_DIRECT
# directly, and `modal/noaa_ais/manifest.py:_connect()` reads either env var.
FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Sized for a 30M-80M-row daily file: download zip (~3-5 GB), stream-parse to
# parquet, upload (~600 MB-1.5 GB) to R2. 90 minutes covers worst-case at
# 5 MB/s sustained from coast.noaa.gov; the AWS S3 mirror (Registry of Open
# Data) would let us drop this materially but the URL plumbing is a v1 swap.
PER_DAY_TIMEOUT_SECONDS = 60 * 90
ORCHESTRATOR_TIMEOUT_SECONDS = 60 * 60 * 8

DEFAULT_MAX_CONCURRENCY = 8

SOURCE_ID = "noaa_ais"
FEED_NAME = "daily_csv"


# ----------------------------------------------------------------------
# Per-day worker
# ----------------------------------------------------------------------


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=PER_DAY_TIMEOUT_SECONDS,
    memory=8192,
    cpu=2,
)
def ingest_one_day(year: int, month: int, day: int) -> dict[str, Any]:
    """Pull, parse, and land one NOAA AIS daily file to R2.

    Idempotent at file grain via ops.ais_pings_ingest_runs: a 'succeeded'
    prior run for the same filename short-circuits with status='skipped'.
    """
    import httpx
    import pyarrow as pa

    from landing import R2Landing
    from noaa_ais.daily_csv_parser import stream_daily_csv_batches
    from noaa_ais.feed_urls import daily_csv_url, daily_zip_filename
    from noaa_ais.manifest import mark_failed, mark_succeeded, open_run

    feed_date = date(year, month, day)
    url = daily_csv_url(feed_date)
    filename = daily_zip_filename(feed_date)
    ingested_at = datetime.now(timezone.utc)
    task_id = os.environ.get("MODAL_TASK_ID")

    run_id = open_run(
        source_filename=filename,
        source_download_url=url,
        file_date=feed_date,
        task_id=task_id,
    )
    if run_id is None:
        return {
            "status": "skipped",
            "reason": "already_succeeded",
            "feed_date": feed_date.isoformat(),
            "source_filename": filename,
        }

    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as zip_tmp:
            # Stream the zip to disk — the file is 3-5 GB, won't fit in RAM
            # comfortably alongside parquet writer state.
            bytes_downloaded = 0
            with httpx.stream(
                "GET",
                url,
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    zip_tmp.write(chunk)
                    bytes_downloaded += len(chunk)
            zip_tmp.flush()

            r2 = R2Landing()
            batches = stream_daily_csv_batches(
                zip_path=zip_tmp.name,
                feed_date=feed_date,
                run_id=run_id,
                ingested_at=ingested_at,
            )
            landing_result = r2.write_streaming(
                source_id=SOURCE_ID,
                feed_name=FEED_NAME,
                feed_date=feed_date,
                run_id=run_id,
                batches=batches,
            )

        mark_succeeded(
            run_id=run_id,
            rows_loaded=landing_result.rows_loaded,
            r2_bucket=landing_result.r2_bucket,
            r2_object_key=landing_result.r2_object_key,
            payload_bytes=landing_result.payload_bytes,
            payload_format=landing_result.payload_format,
            extra_metadata={
                "source_url": url,
                "bytes_downloaded": bytes_downloaded,
            },
        )
        return {
            "status": "succeeded",
            "feed_date": feed_date.isoformat(),
            "source_filename": filename,
            "rows_loaded": landing_result.rows_loaded,
            "bytes_downloaded": bytes_downloaded,
            "r2_object_key": landing_result.r2_object_key,
            "payload_bytes": landing_result.payload_bytes,
        }
    except Exception as exc:
        mark_failed(
            run_id=run_id,
            error_text=f"{type(exc).__name__}: {exc}",
            extra_metadata={"source_url": url},
        )
        raise


# ----------------------------------------------------------------------
# Orchestrator: fan out across a date range
# ----------------------------------------------------------------------


def _enumerate_year(year: int) -> list[tuple[int, int, int]]:
    from calendar import monthrange

    days: list[tuple[int, int, int]] = []
    for month in range(1, 13):
        last = monthrange(year, month)[1]
        for day in range(1, last + 1):
            days.append((year, month, day))
    return days


def _enumerate_range(start: date, end: date) -> list[tuple[int, int, int]]:
    if end < start:
        raise ValueError(f"end={end.isoformat()} precedes start={start.isoformat()}")
    days: list[tuple[int, int, int]] = []
    cursor = start
    while cursor <= end:
        days.append((cursor.year, cursor.month, cursor.day))
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return days


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def ingest_date_range(
    start_iso: str,
    end_iso: str,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> dict[str, Any]:
    """Fan out one ingest_one_day worker per day in [start_iso, end_iso].

    max_concurrency is advisory in Modal v1.4 — the per-bucket cap is
    fixed at @app.function decoration time. Future tuning happens by
    resizing ingest_one_day's parameters or adding a second sibling fn.
    """
    del max_concurrency  # advisory; see docstring

    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    args = _enumerate_range(start, end)

    import sys as _sys
    import uuid as _uuid
    _sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    succeeded = skipped = failed = 0
    failures: list[dict[str, Any]] = []
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="ingest_date_range",
        run_id=run_id,
    ) as hb:
        hb.set_stage("fanout_per_day", {"start": start.isoformat(), "end": end.isoformat(), "days_total": len(args)})
        for result in ingest_one_day.starmap(args, return_exceptions=True):
            if isinstance(result, BaseException):
                failed += 1
                failures.append({"error": f"{type(result).__name__}: {result}"})
                continue
            status = result.get("status")
            if status == "succeeded":
                succeeded += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                failures.append(result)
            hb.update_progress(succeeded=succeeded, skipped=skipped, failed=failed)

    return {
        "run_id": run_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": len(args),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "failures": failures[:50],
    }


# ----------------------------------------------------------------------
# Local entrypoint
# ----------------------------------------------------------------------


@app.local_entrypoint()
def run(
    year: int = 0,
    month: int = 0,
    day: int = 0,
    run_year: bool = False,
    start: str = "",
    end: str = "",
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> None:
    """Operator-driven entry. Three modes:

      Single day:  --year 2024 --month 1 --day 2
      Whole year:  --year 2024 --run-year true
      Date range:  --start 2024-01-01 --end 2024-03-31
    """
    if run_year:
        if not year:
            raise ValueError("--run-year requires --year")
        days = _enumerate_year(year)
        first = date(*days[0])
        last = date(*days[-1])
        result = ingest_date_range.remote(
            start_iso=first.isoformat(),
            end_iso=last.isoformat(),
            max_concurrency=max_concurrency,
        )
        print(result)
        return

    if start and end:
        result = ingest_date_range.remote(
            start_iso=start, end_iso=end, max_concurrency=max_concurrency
        )
        print(result)
        return

    if year and month and day:
        result = ingest_one_day.remote(year=year, month=month, day=day)
        print(result)
        return

    raise ValueError(
        "Provide --year/--month/--day for a single day, "
        "--year + --run-year=true for a whole year, or "
        "--start/--end (YYYY-MM-DD) for a date range."
    )
