#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.geocode_service import GeocodeProgress, close_geocode_service_pool, geocode_and_persist
from app.services.geocode_sources import (
    GeocodeSource,
    close_geocode_sources_pool,
    fetch_source_address_counts,
    fetch_source_addresses,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 2500
DEFAULT_BATCH_SIZE = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch geocode source addresses with Geocodio.")
    parser.add_argument(
        "--source",
        required=True,
        choices=["sba_7a", "sba_504", "usaspending", "fmcsa"],
        help="Source dataset for address extraction.",
    )
    parser.add_argument(
        "--where",
        default=None,
        help="Optional SQL WHERE clause used to filter source records.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of addresses to geocode this run. Use 0 for unlimited (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Geocodio batch size per request (default: {DEFAULT_BATCH_SIZE}).",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = parse_args()

    if args.limit < 0:
        print("ERROR: --limit must be >= 0")
        return 1
    if args.batch_size <= 0:
        print("ERROR: --batch-size must be > 0")
        return 1

    source: GeocodeSource = args.source
    effective_limit = None if args.limit == 0 else args.limit

    try:
        counts = fetch_source_address_counts(source, where_clause=args.where)
        candidates = fetch_source_addresses(
            source,
            where_clause=args.where,
            limit=effective_limit,
        )

        total_queried = len(candidates)
        print(f"Source: {source}")
        print(f"Total eligible source rows: {counts['total_count']}")
        print(f"Skipped (already geocoded): {counts['skipped_count']}")
        print(f"To geocode before limit: {counts['to_geocode_count']}")
        print(f"Total queried this run: {total_queried}")

        if total_queried == 0:
            print("\nRun summary")
            print("total queried: 0")
            print(f"skipped: {counts['skipped_count']}")
            print("geocoded: 0")
            print("failed: 0")
            return 0

        last_milestone = {"value": 0}

        def _on_progress(progress: GeocodeProgress) -> None:
            if progress.processed % 100 == 0 and progress.processed != last_milestone["value"]:
                last_milestone["value"] = progress.processed
                print(
                    "Progress: "
                    f"{progress.processed}/{total_queried} processed, "
                    f"{progress.geocoded} geocoded, {progress.failed} failed"
                )

        summary = geocode_and_persist(
            candidates,
            batch_size=args.batch_size,
            on_progress=_on_progress,
        )

        print("\nRun summary")
        print(f"total queried: {summary.total_queried}")
        print(f"skipped: {counts['skipped_count']}")
        print(f"geocoded: {summary.geocoded}")
        print(f"failed: {summary.failed}")
        if summary.daily_limit_reached:
            print("note: daily limit reached; run ended early.")

        return 0
    finally:
        close_geocode_service_pool()
        close_geocode_sources_pool()


if __name__ == "__main__":
    raise SystemExit(main())
