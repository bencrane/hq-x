"""openFDA Medical Device → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_openfda_device_to_r2.py from R2,
emits three Lance datasets at:
  s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_510k_lance
  s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_pma_lance
  s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_classification_lance

510k dataset (~175K rows):
  Input:  s3://dex-raw-landing-zone/openfda/device/510k/snapshot=*/data.parquet
  BTREE:  k_number (canonical PK)
  Date columns: decision_date, date_received → TRY_CAST typed siblings

PMA dataset (~56K rows, one row per (pma_number, supplement_number)):
  Input:  s3://dex-raw-landing-zone/openfda/device/pma/snapshot=*/data.parquet
  BTREE:  pma_number (canonical PK)
  Date columns: decision_date, date_received → TRY_CAST typed siblings

Classification dataset (~7K rows):
  Input:  s3://dex-raw-landing-zone/openfda/device/classification/snapshot=*/data.parquet
  BTREE:  product_code (canonical PK)

All datasets use mode="overwrite" (full snapshot each cycle — not Volume-King;
510k ~175K rows is trivially cheap to overwrite).

Per CLAUDE.md §"Source ingest invariant" (L197-L215):
  - DuckDB type names written as string literals per L34/L59 (no typing module import)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on canonical PKs
  - No LIST<VARCHAR> — openfda nested object already stored as JSON string (L54)
  - No Content-Encoding: zstd (L42)
  - TRY_CAST date columns (L29)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_openfda_device_lance_emit.py [--variant {510k,pma,classification,all}]
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

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB_510K           = f"s3://{R2_BUCKET}/openfda/device/510k/snapshot=*/data.parquet"
PARQUET_GLOB_PMA            = f"s3://{R2_BUCKET}/openfda/device/pma/snapshot=*/data.parquet"
PARQUET_GLOB_CLASSIFICATION = f"s3://{R2_BUCKET}/openfda/device/classification/snapshot=*/data.parquet"

LANCE_510K_URI           = f"s3://{R2_BUCKET}/polaris-warehouse/openfda/device_510k_lance"
LANCE_PMA_URI            = f"s3://{R2_BUCKET}/polaris-warehouse/openfda/device_pma_lance"
LANCE_CLASSIFICATION_URI = f"s3://{R2_BUCKET}/polaris-warehouse/openfda/device_classification_lance"

POLARIS_NAMESPACE = "openfda"

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
    con.execute("SET memory_limit='8GB'")
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
    """Register a Lance dataset as a Polaris Generic Table."""
    script = Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
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


def emit_510k() -> None:
    """Emit 510k Lance dataset from latest R2 snapshot.

    All columns are raw VARCHAR from c1 ingest. TRY_CAST typed siblings for
    date columns (decision_date, date_received) per L29.
    BTREE on k_number (canonical PK).
    """
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_510K}')"
    ).fetchone()[0]
    logger.info("510k: %d source rows at %s", row_count, PARQUET_GLOB_510K)
    if row_count == 0:
        raise RuntimeError("510k: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            k_number,
            applicant,
            address_1,
            address_2,
            city,
            state,
            zip_code,
            postal_code,
            country_code,
            contact,
            device_name,
            product_code,
            clearance_type,
            decision_code,
            decision_description,
            decision_date,
            TRY_CAST(decision_date AS DATE)   AS decision_date_typed,
            date_received,
            TRY_CAST(date_received AS DATE)   AS date_received_typed,
            advisory_committee,
            advisory_committee_description,
            review_advisory_committee,
            statement_or_summary,
            third_party_flag,
            expedited_review_flag,
            openfda,
            raw_json
        FROM read_parquet('{PARQUET_GLOB_510K}')
        """
    ).fetch_record_batch(rows_per_batch=100_000)

    with lance_commit_lock("device_510k_lance"):
        logger.info("writing 510k Lance dataset to %s ...", LANCE_510K_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_510K_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("510k Lance written: %d rows (version %s)", lance_rows, ds.version)

        logger.info("510k: creating BTREE on k_number ...")
        ds.create_scalar_index("k_number", index_type="BTREE", replace=True)
        logger.info("510k: BTREE on k_number OK")

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("510k: optimize failed (non-fatal): %s", exc)

    logger.info("510k: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_510K_URI)

    _register_polaris(
        "device_510k_lance",
        "openfda.device_510k_lance — openFDA Medical Device 510(k) clearances, "
        "full snapshot, Lance format. BTREE on k_number. ~175K rows. "
        "Source: https://api.fda.gov/download.json.",
    )


def emit_pma() -> None:
    """Emit PMA Lance dataset from latest R2 snapshot.

    One row per (pma_number, supplement_number).
    BTREE on pma_number (canonical PK per directive).
    TRY_CAST typed siblings for decision_date, date_received.
    """
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_PMA}')"
    ).fetchone()[0]
    logger.info("pma: %d source rows at %s", row_count, PARQUET_GLOB_PMA)
    if row_count == 0:
        raise RuntimeError("pma: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            pma_number,
            supplement_number,
            applicant,
            street_1,
            street_2,
            city,
            state,
            zip,
            zip_ext,
            generic_name,
            trade_name,
            product_code,
            advisory_committee,
            advisory_committee_description,
            supplement_type,
            supplement_reason,
            decision_code,
            decision_date,
            TRY_CAST(decision_date AS DATE)   AS decision_date_typed,
            date_received,
            TRY_CAST(date_received AS DATE)   AS date_received_typed,
            docket_number,
            expedited_review_flag,
            ao_statement,
            openfda,
            raw_json
        FROM read_parquet('{PARQUET_GLOB_PMA}')
        """
    ).fetch_record_batch(rows_per_batch=100_000)

    with lance_commit_lock("device_pma_lance"):
        logger.info("writing pma Lance dataset to %s ...", LANCE_PMA_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_PMA_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("pma Lance written: %d rows (version %s)", lance_rows, ds.version)

        logger.info("pma: creating BTREE on pma_number ...")
        ds.create_scalar_index("pma_number", index_type="BTREE", replace=True)
        logger.info("pma: BTREE on pma_number OK")

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("pma: optimize failed (non-fatal): %s", exc)

    logger.info("pma: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_PMA_URI)

    _register_polaris(
        "device_pma_lance",
        "openfda.device_pma_lance — openFDA Medical Device PMA approvals "
        "(one row per (pma_number, supplement_number)), full snapshot, Lance format. "
        "BTREE on pma_number. ~56K rows. Source: https://api.fda.gov/download.json.",
    )


def emit_classification() -> None:
    """Emit classification Lance dataset from latest R2 snapshot.

    Natural PK: product_code. BTREE on product_code.
    No date columns requiring TRY_CAST in classification schema.
    """
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_CLASSIFICATION}')"
    ).fetchone()[0]
    logger.info("classification: %d source rows at %s", row_count, PARQUET_GLOB_CLASSIFICATION)
    if row_count == 0:
        raise RuntimeError("classification: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            product_code,
            device_name,
            device_class,
            regulation_number,
            review_panel,
            review_code,
            medical_specialty,
            medical_specialty_description,
            definition,
            submission_type_id,
            gmp_exempt_flag,
            implant_flag,
            life_sustain_support_flag,
            third_party_flag,
            summary_malfunction_reporting,
            unclassified_reason,
            openfda,
            raw_json
        FROM read_parquet('{PARQUET_GLOB_CLASSIFICATION}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("device_classification_lance"):
        logger.info("writing classification Lance dataset to %s ...", LANCE_CLASSIFICATION_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_CLASSIFICATION_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("classification Lance written: %d rows (version %s)", lance_rows, ds.version)

        logger.info("classification: creating BTREE on product_code ...")
        ds.create_scalar_index("product_code", index_type="BTREE", replace=True)
        logger.info("classification: BTREE on product_code OK")

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("classification: optimize failed (non-fatal): %s", exc)

    logger.info(
        "classification: emit complete — lance_rows=%d uri=%s",
        lance_rows, LANCE_CLASSIFICATION_URI,
    )

    _register_polaris(
        "device_classification_lance",
        "openfda.device_classification_lance — openFDA Medical Device classification, "
        "full snapshot, Lance format. BTREE on product_code. ~7K rows. "
        "Source: https://api.fda.gov/download.json.",
    )


def emit(variants: list[str] | None = None) -> None:
    """Emit Lance datasets for the specified variants (default: all 3).

    Called by the Modal app after R2 ingest.
    """
    if variants is None:
        variants = ["510k", "pma", "classification"]
    for v in variants:
        if v == "510k":
            emit_510k()
        elif v == "pma":
            emit_pma()
        elif v == "classification":
            emit_classification()
        else:
            raise ValueError(f"unknown variant: {v!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="openFDA Medical Device → Lance emit"
    )
    parser.add_argument(
        "--variant",
        choices=["510k", "pma", "classification", "all"],
        default="all",
    )
    args = parser.parse_args()

    if args.variant == "all":
        emit()
    else:
        emit([args.variant])
    return 0


if __name__ == "__main__":
    sys.exit(main())
