"""CSCR (California State Contracts Register) events → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_ca_cscr_events_to_r2.py from R2,
emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/castate/cscr_events_lance

Source columns (per c1): department, department_name, event_id, event_name,
format, type, end_date, status, buyer_name, buyer_email — all VARCHAR.

BTREE indexes:
  event_id                 — canonical key (one row per CSCR event)
  department               — 4-digit agency code, for agency-level filters
  end_date_typed           — typed DATE sibling, parsed from "MM/DD/YYYY HH:MMAM/PM TZ"
  buyer_email_normalized   — LOWER(TRIM(...)) for buyer-side joining

Per CLAUDE.md:
  - DuckDB UDF registration uses string args, not the typing module
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ca_cscr_events_lance_emit.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants ──────────────────────────────────────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB = f"s3://{R2_BUCKET}/castate/cscr-events/snapshot=*/data.parquet"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/castate/cscr_events_lance"

POLARIS_NAMESPACE = "castate"

TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE s3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{os.environ["R2_ENDPOINT"].replace("https://", "")}',
            REGION 'us-east-1',
            URL_STYLE 'path'
        )
        """
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register Lance dataset as a Polaris Generic Table."""
    script = (
        Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    )
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=60)
        logger.info("Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name)
    except subprocess.CalledProcessError as exc:
        logger.warning("Polaris registration failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def emit() -> None:
    """Emit CSCR Lance dataset from latest R2 snapshots."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info("CSCR: %d source rows at %s", row_count, PARQUET_GLOB)
    if row_count == 0:
        raise RuntimeError("CSCR: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    # End-date strings look like "05/19/2026 11:00AM PDT"; the first 10 chars
    # are the MM/DD/YYYY date portion. strptime returns a TIMESTAMP, which
    # TRY_CAST narrows to DATE; invalid input yields NULL.
    reader = con.execute(
        f"""
        SELECT
            department,
            department_name,
            event_id,
            event_name,
            format,
            type,
            end_date,
            TRY_CAST(strptime(SUBSTR(end_date, 1, 10), '%m/%d/%Y') AS DATE) AS end_date_typed,
            status,
            buyer_name,
            buyer_email,
            NULLIF(LOWER(TRIM(buyer_email)), '') AS buyer_email_normalized
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("castate_cscr_events_lance"):
        logger.info("writing CSCR Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("CSCR Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in (
            "event_id",
            "department",
            "end_date_typed",
            "buyer_email_normalized",
        ):
            logger.info("CSCR: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("CSCR: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("CSCR: optimize failed (non-fatal): %s", exc)

    logger.info("CSCR: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "cscr_events_lance",
        "castate.cscr_events_lance — California State Contracts Register events from "
        "the public Cal eProcure Event Search Download button "
        "(caleprocure.ca.gov/pages/Events-BS3/event-search.aspx), one-shot manual ingest. "
        "BTREE on event_id, department, end_date_typed, buyer_email_normalized.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSCR (Cal eProcure Event Search) → Lance emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        con = _duckdb_conn()
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
        ).fetchone()[0]
        logger.info("DRY-RUN CSCR: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
