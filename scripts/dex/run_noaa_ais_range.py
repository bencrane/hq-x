#!/usr/bin/env python3
"""Local runner — bulk ingest a date range of NOAA AIS daily files → R2.

Sequential by default (one day at a time, no concurrency). For real
parallelism use Modal (modal/noaa_ais_ingest_app.py::ingest_date_range);
this script is the single-host fallback for ad-hoc runs.

    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_noaa_ais_range.py \\
            --start 2024-01-01 --end 2024-01-31

A failure on any single day is logged and the loop continues — the
manifest's 'failed' row is the recovery surface (re-run the same range,
the 'succeeded' rows short-circuit, only failed/missing days re-fetch).

Use --skip-failed to also short-circuit prior 'failed' rows; default is to
retry them (the failure mode is usually transient — coast.noaa.gov 503s,
zip-mid-stream truncation).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_noaa_ais_day import ingest_one_day  # noqa: E402


def _enumerate(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end={end.isoformat()} precedes start={start.isoformat()}")
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor = cursor + timedelta(days=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = _enumerate(start, end)

    print(
        f"[noaa-ais-range] {len(days)} day(s) from {start.isoformat()} "
        f"to {end.isoformat()}",
        flush=True,
    )

    succeeded = skipped = failed = 0
    failures: list[dict] = []
    for d in days:
        try:
            result = ingest_one_day(year=d.year, month=d.month, day=d.day)
            status = result.get("status")
            if status == "succeeded":
                succeeded += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            failures.append(
                {"date": d.isoformat(), "error": f"{type(exc).__name__}: {exc}"}
            )
            print(
                f"[noaa-ais-range] CONTINUE after {d.isoformat()} failure",
                flush=True,
            )

    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": len(days),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "failures": failures[:50],
    }
    print(f"[noaa-ais-range] DONE {json.dumps(summary)}", flush=True)


if __name__ == "__main__":
    main()
