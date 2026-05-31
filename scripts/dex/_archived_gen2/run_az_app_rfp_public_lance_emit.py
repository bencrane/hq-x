"""Arizona APP Public RFP Browse → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_az_app_rfp_public_to_r2.py from R2,
emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/azstate/app_rfp_public_lance

Source columns (13, all VARCHAR; lowercase snake_case from the scraper):
  rfp_id, code, label, publication_begin_date_utc7, commodity, agency,
  publication_end_date_utc7, status, rfx_awarded, remaining_time,
  begin_utc7, end_utc7, detail_url

BTREE indexes (per audit plan):
  code                  — canonical PK (BPM number)
  rfp_id                — internal numeric ID (detail-URL key)
  agency_normalized     — entity-name normalized agency
  end_typed             — try_strptime(end_utc7) → DATE for time-based filters

Per CLAUDE.md:
  - DuckDB UDF registration uses STRING type names per HEAD 6df6d840 (string args, not the typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (try_strptime + TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_az_app_rfp_public_lance_emit.py [--apply]
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
from scripts._lib.entity_name_normalize import normalize_entity_name

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB = f"s3://{R2_BUCKET}/azstate/app-rfp-public/snapshot=*/data.parquet"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/azstate/app_rfp_public_lance"

# azstate namespace per state-procurement runbook §"Namespacing conventions"
POLARIS_NAMESPACE = "azstate"

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
    # DuckDB UDF registration: STRING type names per HEAD 6df6d840 (string args, not the typing module)
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
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
    """Emit AZ APP RFP public Lance dataset from latest R2 snapshot."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info("AZ APP RFP: %d source rows at %s", row_count, PARQUET_GLOB)
    if row_count == 0:
        raise RuntimeError("AZ APP RFP: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    # Date format in source: 'M/D/YYYY h:mm:ss AM/PM' (UTC-7).
    # try_strptime with multiple format hints returns NULL on parse failure.
    reader = con.execute(
        f"""
        SELECT
            rfp_id,
            code,
            label,
            publication_begin_date_utc7,
            CAST(try_strptime(publication_begin_date_utc7, '%-m/%-d/%Y %-I:%M:%S %p') AS DATE)
                                                                     AS publication_begin_date_typed,
            commodity,
            agency,
            py_normalize_entity(agency)                              AS agency_normalized,
            publication_end_date_utc7,
            CAST(try_strptime(publication_end_date_utc7, '%-m/%-d/%Y %-I:%M:%S %p') AS DATE)
                                                                     AS publication_end_date_typed,
            status,
            rfx_awarded,
            remaining_time,
            begin_utc7,
            CAST(try_strptime(begin_utc7, '%-m/%-d/%Y %-I:%M:%S %p') AS DATE)
                                                                     AS begin_typed,
            end_utc7,
            CAST(try_strptime(end_utc7, '%-m/%-d/%Y %-I:%M:%S %p') AS DATE)
                                                                     AS end_typed,
            detail_url
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("az_app_rfp_public_lance"):
        logger.info("writing AZ APP RFP Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("AZ APP RFP Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in ("code", "rfp_id", "agency_normalized", "end_typed"):
            logger.info("AZ APP RFP: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("AZ APP RFP: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("AZ APP RFP: optimize failed (non-fatal): %s", exc)

    logger.info("AZ APP RFP: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "app_rfp_public_lance",
        "azstate.app_rfp_public_lance — Arizona Procurement Portal (Ivalua) public RFP "
        "browse, all open state-agency solicitations, daily refresh, ~150 rows per snapshot. "
        "BTREE on code, rfp_id, agency_normalized, end_typed. "
        "Source: https://app.az.gov/page.aspx/en/rfp/request_browse_public.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Arizona APP RFP public → Lance emit"
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
        logger.info("DRY-RUN AZ APP RFP: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
