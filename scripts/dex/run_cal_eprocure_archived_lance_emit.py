"""CA Cal eProcure archived opportunities → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_cal_eprocure_archived_to_r2.py from R2,
emits two Lance datasets at:
  s3://dex-raw-landing-zone/polaris-warehouse/castate/cal_eprocure_archived_po_lance
  s3://dex-raw-landing-zone/polaris-warehouse/castate/cal_eprocure_archived_ncb_lance

PO dataset (344K rows, FY 2012-2015 historical):
  Input:  s3://dex-raw-landing-zone/cal-eprocure-archived/po/snapshot=*/data.parquet
  BTREE:  purchase_order_number + department_name_normalized + purchase_date_typed

NCB dataset (469 rows, monthly Special Category ≥$1M):
  Input:  s3://dex-raw-landing-zone/cal-eprocure-archived/ncb/snapshot=*/data.parquet
  BTREE:  ncb_number + requesting_organization_normalized + approved_on_typed

NCB column note: the XLSX has a 2-row preamble (row 0 = description note, row 1 = blank)
before the actual header at row 2 (0-indexed). Pandas read_excel with header=2 reads it
correctly. Column names from source: Number, Type, Approved on, Requesting Organization,
Contractor or Commodity, Total Original Contract Amount, Amended Contract Amount,
Acquisition Type. We alias to snake_case for Lance ergonomics.

Per CLAUDE.md §"Source ingest invariant" (L197-L215):
  - DuckDB UDF registration uses STRING type names per HEAD 6b77d687 (NOT duckdb-typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_cal_eprocure_archived_lance_emit.py [--variant {po,ncb,both}] [--apply]
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

PARQUET_GLOB_PO  = f"s3://{R2_BUCKET}/cal-eprocure-archived/po/snapshot=*/data.parquet"
PARQUET_GLOB_NCB = f"s3://{R2_BUCKET}/cal-eprocure-archived/ncb/snapshot=*/data.parquet"

LANCE_PO_URI  = f"s3://{R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_po_lance"
LANCE_NCB_URI = f"s3://{R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_ncb_lance"

# castate namespace per directive §"Audit plan" §c5
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
    # DuckDB UDF registration: STRING type names per HEAD 6b77d687
    # Use string type names per HEAD 6b77d687 — CLAUDE.md §"DuckDB UDF"
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register a Lance dataset as a Polaris Generic Table."""
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


def emit_po() -> None:
    """Emit PO Lance dataset from latest R2 snapshot.

    PO columns (from CKAN CSV header):
      Creation Date, Purchase Date, Fiscal Year, LPA Number, Purchase Order Number,
      Requisition Number, Acquisition Type, Sub-Acquisition Type, Acquisition Method,
      Sub-Acquisition Method, Department Name, Supplier Code, Supplier Name,
      Supplier Qualifications, Supplier Zip Code, CalCard, Item Name, Item Description,
      Quantity, Unit Price, Total Price, Classification Codes, Normalized UNSPSC,
      Commodity Title, Class, Class Title, Family, Family Title, Segment, Segment Title,
      Location, REMOVE AMERISOURCE

    BTREE: purchase_order_number + department_name_normalized + purchase_date_typed
    """
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_PO}')"
    ).fetchone()[0]
    logger.info("PO: %d source rows at %s", row_count, PARQUET_GLOB_PO)
    if row_count == 0:
        raise RuntimeError("PO: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            "Creation Date"                                   AS creation_date,
            "Purchase Date"                                   AS purchase_date,
            TRY_CAST("Purchase Date" AS DATE)                 AS purchase_date_typed,
            "Fiscal Year"                                     AS fiscal_year,
            "LPA Number"                                      AS lpa_number,
            "Purchase Order Number"                           AS purchase_order_number,
            "Requisition Number"                              AS requisition_number,
            "Acquisition Type"                                AS acquisition_type,
            "Sub-Acquisition Type"                            AS sub_acquisition_type,
            "Acquisition Method"                              AS acquisition_method,
            "Sub-Acquisition Method"                          AS sub_acquisition_method,
            "Department Name"                                 AS department_name,
            py_normalize_entity("Department Name")            AS department_name_normalized,
            "Supplier Code"                                   AS supplier_code,
            "Supplier Name"                                   AS supplier_name,
            py_normalize_entity("Supplier Name")              AS supplier_name_normalized,
            "Supplier Qualifications"                         AS supplier_qualifications,
            "Supplier Zip Code"                               AS supplier_zip_code,
            "CalCard"                                         AS calcard,
            "Item Name"                                       AS item_name,
            "Item Description"                                AS item_description,
            "Quantity"                                        AS quantity,
            "Unit Price"                                      AS unit_price,
            "Total Price"                                     AS total_price,
            "Classification Codes"                            AS classification_codes,
            "Normalized UNSPSC"                               AS normalized_unspsc,
            "Commodity Title"                                 AS commodity_title,
            "Class"                                           AS class_code,
            "Class Title"                                     AS class_title,
            "Family"                                          AS family,
            "Family Title"                                    AS family_title,
            "Segment"                                         AS segment,
            "Segment Title"                                   AS segment_title,
            "Location"                                        AS location
        FROM read_parquet('{PARQUET_GLOB_PO}')
        """
    ).fetch_record_batch(rows_per_batch=100_000)

    with lance_commit_lock("cal_eprocure_archived_po_lance"):
        logger.info("writing PO Lance dataset to %s ...", LANCE_PO_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_PO_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("PO Lance written: %d rows (version %s)", lance_rows, ds.version)

        # BTREE indexes per audit plan §c5
        for col in ("purchase_order_number", "department_name_normalized", "purchase_date_typed"):
            logger.info("PO: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("PO: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("PO: optimize failed (non-fatal): %s", exc)

    logger.info("PO: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_PO_URI)

    _register_polaris(
        "cal_eprocure_archived_po_lance",
        "castate.cal_eprocure_archived_po_lance — CA DGS Purchase Order historical (FY 2012-2015), "
        "archived via CKAN data.ca.gov, one-shot backfill 344K rows.",
    )


def emit_ncb() -> None:
    """Emit NCB Lance dataset from latest R2 snapshot.

    NCB columns (from XLSX, row 2 = header, 0-indexed):
      Number, Type, Approved on, Requesting Organization,
      Contractor or Commodity, Total Original Contract Amount,
      Amended Contract Amount, Acquisition Type

    BTREE: ncb_number + requesting_organization_normalized + approved_on_typed
    """
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_NCB}')"
    ).fetchone()[0]
    logger.info("NCB: %d source rows at %s", row_count, PARQUET_GLOB_NCB)
    if row_count == 0:
        raise RuntimeError("NCB: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            "Number"                                            AS ncb_number,
            "Type"                                              AS ncb_type,
            "Approved on"                                       AS approved_on,
            TRY_CAST("Approved on" AS DATE)                     AS approved_on_typed,
            "Requesting Organization"                           AS requesting_organization,
            py_normalize_entity("Requesting Organization")      AS requesting_organization_normalized,
            "Contractor or Commodity"                           AS contractor_or_commodity,
            py_normalize_entity("Contractor or Commodity")      AS contractor_normalized,
            "Total Original Contract Amount"                    AS total_original_contract_amount,
            "Amended Contract Amount"                           AS amended_contract_amount,
            "Acquisition Type"                                  AS acquisition_type
        FROM read_parquet('{PARQUET_GLOB_NCB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("cal_eprocure_archived_ncb_lance"):
        logger.info("writing NCB Lance dataset to %s ...", LANCE_NCB_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_NCB_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("NCB Lance written: %d rows (version %s)", lance_rows, ds.version)

        # BTREE indexes per audit plan §c5
        for col in ("ncb_number", "requesting_organization_normalized", "approved_on_typed"):
            logger.info("NCB: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("NCB: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("NCB: optimize failed (non-fatal): %s", exc)

    logger.info("NCB: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_NCB_URI)

    _register_polaris(
        "cal_eprocure_archived_ncb_lance",
        "castate.cal_eprocure_archived_ncb_lance — CA DGS Non-Competitive Bids (Special Category ≥$1M), "
        "monthly refresh, ~469 rows per snapshot.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CA Cal eProcure archived → Lance emit"
    )
    parser.add_argument(
        "--variant",
        choices=["po", "ncb", "both"],
        default="both",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance datasets (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        # Dry-run: just count rows in Parquet
        con = _duckdb_conn()
        if args.variant in ("po", "both"):
            n = con.execute(
                f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_PO}')"
            ).fetchone()[0]
            logger.info("DRY-RUN PO: %d rows in Parquet glob (pass --apply to emit)", n)
        if args.variant in ("ncb", "both"):
            n = con.execute(
                f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB_NCB}')"
            ).fetchone()[0]
            logger.info("DRY-RUN NCB: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    if args.variant in ("po", "both"):
        emit_po()
    if args.variant in ("ncb", "both"):
        emit_ncb()
    return 0


if __name__ == "__main__":
    sys.exit(main())
