#!/usr/bin/env python3
"""Local runner — pull one NOAA AIS daily file → R2 (no Modal).

Mirrors modal/noaa_ais_ingest_app.py::ingest_one_day but runs in-process so
the operator can drive the ingest from a Doppler-injected shell:

    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_noaa_ais_day.py --year 2024 --month 1 --day 2

Idempotent at file grain via ops.ais_pings_ingest_runs (UNIQUE on
source_filename); a 'succeeded' prior row short-circuits with status='skipped'.

For ranges, wrap with a shell loop or invoke from scripts/run_noaa_ais_range.py
(see modal/noaa_ais_ingest_app.py::ingest_date_range for the parallelization
shape — locally, sequential or background-process fan-out).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

# Add apps/data-engine-x/modal to sys.path so `landing` and `noaa_ais` resolve
# the same way they would on a Modal worker (where add_local_dir mounts each
# package at /root/<name>).
HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parent
sys.path.insert(0, str(APP_ROOT / "modal"))

import httpx  # noqa: E402

from landing import R2Landing  # noqa: E402
from noaa_ais.daily_csv_parser import stream_daily_csv_batches  # noqa: E402
from noaa_ais.feed_urls import daily_csv_url, daily_zip_filename  # noqa: E402
from noaa_ais.manifest import mark_failed, mark_succeeded, open_run  # noqa: E402

SOURCE_ID = "noaa_ais"
FEED_NAME = "daily_csv"


def _human_mb(b: int) -> str:
    return f"{b / (1 << 20):.1f} MB"


def ingest_one_day(year: int, month: int, day: int) -> dict:
    feed_date = date(year, month, day)
    url = daily_csv_url(feed_date)
    filename = daily_zip_filename(feed_date)
    ingested_at = datetime.now(timezone.utc)
    task_id = f"local-{ingested_at.strftime('%Y-%m-%dT%H:%M:%S')}"

    print(f"[noaa-ais] feed_date={feed_date.isoformat()} url={url}", flush=True)

    run_id = open_run(
        source_filename=filename,
        source_download_url=url,
        file_date=feed_date,
        task_id=task_id,
    )
    if run_id is None:
        print(
            f"[noaa-ais] SKIP filename={filename} (already 'succeeded')",
            flush=True,
        )
        return {"status": "skipped", "feed_date": feed_date.isoformat()}

    print(f"[noaa-ais] run_id={run_id} downloading zip…", flush=True)

    download_started = time.perf_counter()
    bytes_downloaded = 0

    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as zip_tmp:
            with httpx.stream(
                "GET",
                url,
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                last_print = 0
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    zip_tmp.write(chunk)
                    bytes_downloaded += len(chunk)
                    if bytes_downloaded - last_print > 200 * (1 << 20):
                        print(
                            f"[noaa-ais]   downloaded {_human_mb(bytes_downloaded)}",
                            flush=True,
                        )
                        last_print = bytes_downloaded
            zip_tmp.flush()
            download_seconds = time.perf_counter() - download_started

            print(
                f"[noaa-ais] download done — {_human_mb(bytes_downloaded)} in "
                f"{download_seconds:.1f}s. Streaming → R2…",
                flush=True,
            )

            r2 = R2Landing()
            batches = stream_daily_csv_batches(
                zip_path=zip_tmp.name,
                feed_date=feed_date,
                run_id=run_id,
                ingested_at=ingested_at,
            )
            landing_started = time.perf_counter()
            landing_result = r2.write_streaming(
                source_id=SOURCE_ID,
                feed_name=FEED_NAME,
                feed_date=feed_date,
                run_id=run_id,
                batches=batches,
            )
            landing_seconds = time.perf_counter() - landing_started

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
                "download_seconds": round(download_seconds, 2),
                "landing_seconds": round(landing_seconds, 2),
                "runner": "scripts/run_noaa_ais_day.py",
            },
        )

        print(
            f"[noaa-ais] DONE rows={landing_result.rows_loaded:,} "
            f"parquet={_human_mb(landing_result.payload_bytes or 0)} "
            f"download={download_seconds:.1f}s land={landing_seconds:.1f}s "
            f"key={landing_result.r2_object_key}",
            flush=True,
        )
        return {
            "status": "succeeded",
            "feed_date": feed_date.isoformat(),
            "rows_loaded": landing_result.rows_loaded,
            "bytes_downloaded": bytes_downloaded,
            "payload_bytes": landing_result.payload_bytes,
            "r2_object_key": landing_result.r2_object_key,
            "run_id": str(run_id),
        }
    except Exception as exc:
        mark_failed(
            run_id=run_id,
            error_text=f"{type(exc).__name__}: {exc}",
            extra_metadata={
                "source_url": url,
                "bytes_downloaded": bytes_downloaded,
                "runner": "scripts/run_noaa_ais_day.py",
            },
        )
        print(f"[noaa-ais] FAIL {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--day", type=int, required=True)
    args = ap.parse_args()
    ingest_one_day(year=args.year, month=args.month, day=args.day)


if __name__ == "__main__":
    main()
