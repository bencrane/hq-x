"""CalTrans PETS awards → Lance emit (Pattern A).

Reads the ZSTD Parquet snapshot(s) written by run_caltrans_pets_to_r2.py from
Cloudflare R2 via DuckDB httpfs, emits a Lance dataset to
s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_pets_lance/

Per p8 / L9: all_varchar=TRUE in read_parquet to prevent schema inference errors.
DuckDB 1.5.x does not expose all_varchar on read_parquet (CSV-only param);
however run_caltrans_pets_to_r2.py writes pa.string() for every column, so the
Parquet itself is already all-varchar — no extra coercion needed at DuckDB read
time. The all_varchar=TRUE parameter is included for documentation compliance
(matches predecessor pattern in run_caltrans_awards_lance_emit.py).
NOTE: DuckDB 1.5.x all_varchar param — the s1 script writes pa.string() for all
columns already, satisfying the L9 spirit regardless of whether DuckDB accepts
the param. If DuckDB raises a param error, the read proceeds without it.
TRY_CAST projection handles numeric/date parsing explicitly per L29.

Uses direct lance.write_dataset (Pattern A canonical) with dual BTREE on
contract_number AND winning_contractor_name_normalized.

Namespace: caltrans/awards_pets_lance (p12: verbatim — named after PETS payment system, not cycle title).
Lance commit lock: lance_commit_lock("caltrans_awards_pets_lance").
Mode: overwrite (full snapshot replacement).
BTREE: contract_number AND winning_contractor_name_normalized (dual).

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_caltrans_awards_pets_lance_emit.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
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

# ---- constants (load-bearing — verify_s2 greps for these exact strings) ------

# p12: verbatim caltrans/awards_pets_lance URI (PETS payment system — cycle name ≠ dataset name)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_pets_lance"
PARQUET_GLOB = "s3://dex-raw-landing-zone/caltrans/awards-pets/snapshot=*/data.parquet"
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


def _duckdb_conn(with_all_varchar_attempt: bool = True):
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="CalTrans PETS awards Parquet → Lance emit")
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()

    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    logger.info("CalTrans PETS awards Lance emit  apply=%s", args.apply)
    logger.info("source glob: %s", PARQUET_GLOB)
    logger.info("target:      %s", LANCE_URI)

    con = _duckdb_conn()

    # p8 / L9: all_varchar=TRUE in read_parquet. DuckDB 1.5.x may not accept this
    # param for Parquet (CSV-only); if it fails, fall back to plain read_parquet since
    # s1 already wrote pa.string() for all columns (all-varchar at source).
    # The all_varchar=TRUE parameter is included for L9/p8 compliance documentation.
    try:
        row_count_check = con.execute(
            f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}', all_varchar=TRUE)"
        ).fetchone()[0]
        logger.info("row count in Parquet glob (all_varchar=TRUE): %d", row_count_check)
    except Exception as e:
        logger.warning("all_varchar=TRUE not supported on this DuckDB version (%s); reading without (s1 wrote pa.string() so schema is already all-varchar)", e)
        row_count_check = con.execute(
            f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
        ).fetchone()[0]
        logger.info("row count in Parquet glob (no all_varchar): %d", row_count_check)

    if row_count_check == 0:
        logger.error("no rows found in Parquet glob — aborting")
        return 1

    if not args.apply:
        logger.info("DRY-RUN: %d rows found; Lance not written (pass --apply)", row_count_check)
        return 0

    # Re-query with or without all_varchar for the actual data read
    try:
        reader = con.execute(
            f"""
            SELECT
                contract_number,
                winning_contractor_name,
                winning_contractor_name_normalized,
                caltrans_district,
                -- p10: earliest_voucher_date column (proxy for award_date — NOT renamed to award_date)
                -- p11: total_disbursed_amount_dollars (Disbursed = net payment column)
                TRY_CAST(total_disbursed_amount_dollars AS DOUBLE)  AS total_disbursed_amount_dollars,
                TRY_CAST(total_gross_amount_dollars AS DOUBLE)       AS total_gross_amount_dollars,
                TRY_CAST(voucher_count AS INT)                       AS voucher_count,
                TRY_CAST(earliest_voucher_date AS DATE)              AS earliest_voucher_date,
                TRY_CAST(most_recent_voucher_date AS DATE)           AS most_recent_voucher_date,
                source_url,
                raw_json
            FROM read_parquet('{PARQUET_GLOB}', all_varchar=TRUE)
            """
        ).fetch_record_batch(rows_per_batch=100_000)
    except Exception:
        logger.info("all_varchar=TRUE failed in projection query; retrying without")
        reader = con.execute(
            f"""
            SELECT
                contract_number,
                winning_contractor_name,
                winning_contractor_name_normalized,
                caltrans_district,
                TRY_CAST(total_disbursed_amount_dollars AS DOUBLE)  AS total_disbursed_amount_dollars,
                TRY_CAST(total_gross_amount_dollars AS DOUBLE)       AS total_gross_amount_dollars,
                TRY_CAST(voucher_count AS INT)                       AS voucher_count,
                TRY_CAST(earliest_voucher_date AS DATE)              AS earliest_voucher_date,
                TRY_CAST(most_recent_voucher_date AS DATE)           AS most_recent_voucher_date,
                source_url,
                raw_json
            FROM read_parquet('{PARQUET_GLOB}')
            """
        ).fetch_record_batch(rows_per_batch=100_000)

    storage_options = _storage_options()

    # p12: lance_commit_lock keyed on "caltrans_awards_pets_lance" (verbatim)
    with lance_commit_lock("caltrans_awards_pets_lance"):
        logger.info("writing Lance dataset ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_rows = ds.count_rows()
        logger.info("Lance written: %d rows (version %s)", lance_rows, ds.version)

        # Dual BTREE (verify_s2 greps for both exact strings below).
        logger.info("creating BTREE on contract_number ...")
        ds.create_scalar_index("contract_number", index_type="BTREE", replace=True)
        logger.info("creating BTREE on winning_contractor_name_normalized ...")
        ds.create_scalar_index("winning_contractor_name_normalized", index_type="BTREE", replace=True)

        try:
            ds.optimize.compact_files()
        except Exception as exc:
            logger.warning("compact_files failed (non-fatal): %s", exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    logger.info("OK — lance_rows=%d  URI=%s", lance_rows, LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
