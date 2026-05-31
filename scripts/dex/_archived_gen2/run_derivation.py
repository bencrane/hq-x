#!/usr/bin/env python3
"""CLI entrypoint for the derivation factory.

Usage:
    # List all derivations declared in the config
    doppler run --project hq-all --config prd -- \\
        uv run --with pyyaml --with duckdb \\
        python apps/data-engine-x/scripts/run_derivation.py --list

    # Dry-run a derivation (counts + smoke gates only, no R2 write)
    doppler run --project hq-all --config prd -- \\
        uv run --with pyyaml --with duckdb \\
        python apps/data-engine-x/scripts/run_derivation.py \\
            --name factory_poc_carrier_essentials --dry-run

    # Apply a derivation (writes Parquet to R2)
    doppler run --project hq-all --config prd -- \\
        uv run --with pyyaml --with duckdb \\
        python apps/data-engine-x/scripts/run_derivation.py \\
            --name factory_poc_carrier_essentials --apply

    # Apply with explicit input snapshot date override
    doppler run --project hq-all --config prd -- \\
        uv run --with pyyaml --with duckdb \\
        python apps/data-engine-x/scripts/run_derivation.py \\
            --name factory_poc_carrier_essentials --apply \\
            --snapshot 2026-05-09

The factory module is at scripts/_lib/derivation_factory.py.
The config is at scripts/_config/derivations.yaml.

This is the BUILD layer of the derivation factory. The RW serve layer
(admit source, admit MV) is a separate concern that varies by substrate
choice (RW Cloud paid / self-host RW / replace with DuckDB+serve-layer).
See reports/2026-05-11-substrate-evaluation-tradeoff-matrix.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.derivation_factory import DerivationFactory  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"


def _upsert_current_snapshot(dataset_name: str, snapshot_date: str,
                             snapshot_uri: str, rows_written: int) -> None:
    """Upsert ops.current_snapshots so the serve layer sees the new pointer."""
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not db_url:
        raise SystemExit(
            "FAIL: DEX_DB_URL_DIRECT not set — cannot upsert ops.current_snapshots"
        )
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.current_snapshots
                  (dataset_name, snapshot_date, snapshot_uri, rows_written)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dataset_name) DO UPDATE SET
                  snapshot_date = EXCLUDED.snapshot_date,
                  snapshot_uri  = EXCLUDED.snapshot_uri,
                  rows_written  = EXCLUDED.rows_written,
                  updated_at    = now()
                """,
                (dataset_name, snapshot_date, snapshot_uri, rows_written),
            )

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "_config" / "derivations.yaml"),
        help="Path to derivations.yaml (default: scripts/_config/derivations.yaml)",
    )
    parser.add_argument("--list", action="store_true",
                        help="List all derivation names in the config and exit.")
    parser.add_argument("--name", default=None,
                        help="Name of the derivation to run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Execute up to row-count smoke gate, but do not write to R2.")
    parser.add_argument("--apply", action="store_true",
                        help="Execute and write output Parquet to R2.")
    parser.add_argument("--snapshot", default=None,
                        help="Override input snapshot date (YYYY-MM-DD). "
                             "Default: latest detected.")
    args = parser.parse_args()

    factory = DerivationFactory(args.config)

    if args.list:
        for n in factory.list():
            d = factory.get(n)
            print(f"{n}: {d['description'].strip().splitlines()[0]}")
        return 0

    if not args.name:
        parser.error("--name is required (or use --list)")
    if not (args.dry_run or args.apply):
        parser.error("--dry-run or --apply is required")
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")

    result = factory.run(
        args.name,
        dry_run=args.dry_run,
        snapshot_date=args.snapshot,
    )
    if result.dry_run:
        print(f"[{result.name}] DRY-RUN: {result.rows_written:,} rows in {result.duration_seconds:.1f}s")
    else:
        snapshot_uri = f"r2://{R2_BUCKET}/{result.output_key}"
        _upsert_current_snapshot(
            dataset_name=result.name,
            snapshot_date=result.snapshot_date,
            snapshot_uri=snapshot_uri,
            rows_written=result.rows_written,
        )
        print(f"[{result.name}] APPLIED: wrote {result.rows_written:,} rows → {result.output_key} in {result.duration_seconds:.1f}s")
        print(f"[{result.name}] POINTER: ops.current_snapshots[{result.name}] = {snapshot_uri}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
