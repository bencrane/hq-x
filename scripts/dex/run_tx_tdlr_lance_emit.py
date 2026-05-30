"""TDLR All-Licenses → Lance emit (Pattern A, one-off operator-fired).

Reads the operator-supplied Texas Department of Licensing & Regulation (TDLR)
"All Licenses" CSV export and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/licensure/tx_tdlr_lance

R2 Parquet intermediate at:
    s3://dex-raw-landing-zone/tx-tdlr/release={release}/data.parquet

Columns:
  - 18 verbatim from CSV (all VARCHAR, per all_varchar=TRUE / L9 "emit verbatim";
    header names preserved exactly — incl. spaces/commas/parens — per Source
    ingest invariant §"TRUE 1:1 column mirror". Confirmed 18 cols 2026-05-29):
      LICENSE TYPE, LICENSE NUMBER, BUSINESS COUNTY, BUSINESS NAME,
      BUSINESS ADDRESS-LINE1, BUSINESS ADDRESS-LINE2, BUSINESS CITY, STATE ZIP,
      BUSINESS TELEPHONE, LICENSE EXPIRATION DATE (MMDDCCYY), OWNER NAME,
      MAILING ADDRESS LINE1, MAILING ADDRESS LINE2,
      MAILING ADDRESS CITY, STATE ZIP, MAILING ADDRESS COUNTY, OWNER TELEPHONE,
      LICENSE SUBTYPE, CONTINUING EDUCATION FLAG, Business Mailing
  - business_name_normalized (VARCHAR, additive computed column via
    normalize_entity_name DuckDB UDF per L34).

BTREE indices on LICENSE NUMBER + business_name_normalized + LICENSE TYPE
(three consecutive ds.create_scalar_index calls — inline write_dataset pattern,
NOT via the single-btree helper; mirrors run_cslb_licensees_lance_emit.py).
LICENSE TYPE is indexed because TDLR is an omnibus regulator (84 license types);
the BTREE makes per-trade filtering (Electrical Contractor, A/C Contractor,
Master Electrician, Appliance/Elevator/Sign contractors, etc.) a push-down read.

One-off: no Modal, no cron, no idempotency loop, no audit ledger.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_tx_tdlr_lance_emit.py \\
        --csv-path "/Users/benjamincrane/Downloads/TDLR_-_All_Licenses_20260529.csv" \\
        --release 2026-05-29

Precedent: PR #480 run_cslb_licensees_lance_emit.py (dual-BTREE inline pattern);
extended to triple-BTREE for the omnibus license-type filter.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Resolve scripts/ as a package when run directly (not via installed module).
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import boto3
import duckdb
import lance

from scripts._lib.entity_name_normalize import normalize_entity_name
from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "tx_tdlr_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/licensure/tx_tdlr_lance"

# Validator: row floor at 950K to accommodate minor CSV-sniff drift between
# probe + run (probe parsed 962,445 rows clean on 2026-05-29).
MIN_ROW_FLOOR = 950_000

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# R2 helpers (verbatim from run_cslb_licensees_lance_emit.py)
# ---------------------------------------------------------------------------

def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb() -> duckdb.DuckDBPyConnection:
    """Connect DuckDB + install httpfs + create R2 secret."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


# ---------------------------------------------------------------------------
# Main emit logic
# ---------------------------------------------------------------------------

def emit(csv_path: str, release: str) -> dict:
    """Pattern A Lance emit: CSV → ZSTD Parquet → R2 → Lance + triple BTREE.

    Steps mirror run_cslb_licensees_lance_emit.py:
    1. Set LANCE env vars.
    2. DuckDB connect + register normalize_entity_name UDF.
    3. COPY CSV to local ZSTD Parquet (all_varchar=TRUE, + business_name_normalized).
    4. Row-count floor check.
    5. Upload Parquet to R2 (ContentType only, no ContentEncoding per L42).
    6. Read local Parquet back for the Lance write.
    7. lance.write_dataset inside lance_commit_lock.
    8. Three BTREE indices: LICENSE NUMBER + business_name_normalized + LICENSE TYPE.
    9. compact_files + cleanup_old_versions.
    """
    # Step 1: Lance env setup
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    local_parquet = f"/tmp/tx-tdlr-{release}.parquet"
    r2_key = f"tx-tdlr/release={release}/data.parquet"
    r2_bucket = "dex-raw-landing-zone"
    parquet_uri = f"s3://{r2_bucket}/{r2_key}"

    # Step 2: DuckDB connect + register UDF.
    # null_handling="special" required: normalize_entity_name returns None for
    # generic/sole-prop names (per L33). Without "special", DuckDB raises
    # InvalidInputError when the UDF returns NULL.
    con = _connect_duckdb()
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        [duckdb.type("VARCHAR")],
        duckdb.type("VARCHAR"),
        null_handling="special",
    )

    # Step 3: COPY CSV → local ZSTD Parquet.
    # all_varchar=TRUE per L9 (preserves verbatim string types); header=TRUE
    # preserves the source column names exactly. py_normalize_entity adds the
    # 19th column (VARCHAR).
    logger.info("COPY CSV → local Parquet: %s", local_parquet)
    t0 = time.time()
    con.execute(
        f"""
        COPY (
            SELECT
                *,
                py_normalize_entity("BUSINESS NAME") AS business_name_normalized
            FROM read_csv(
                '{csv_path}',
                all_varchar=TRUE,
                header=TRUE,
                ignore_errors=FALSE
            )
        ) TO '{local_parquet}' (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            ROW_GROUP_SIZE 100000
        )
        """
    )
    copy_dur = time.time() - t0
    logger.info("COPY done in %.1fs", copy_dur)

    # Step 4: Row-count floor check
    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    logger.info("Parquet row count: %d (floor %d)", row_count, MIN_ROW_FLOOR)
    if row_count < MIN_ROW_FLOOR:
        msg = f"FAIL: Parquet row count {row_count} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        sys.exit(1)

    # Step 5: Upload Parquet to R2.
    # ContentType only — NO ContentEncoding per L42; plain .parquet extension.
    logger.info("Uploading to R2: s3://%s/%s", r2_bucket, r2_key)
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    t_upload = time.time()
    s3.upload_file(
        local_parquet,
        r2_bucket,
        r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    upload_dur = time.time() - t_upload
    logger.info("R2 upload done in %.1fs", upload_dur)

    # Step 6: Read local Parquet for the Lance write.
    # Reading back from R2 via DuckDB httpfs is avoided because the
    # Hive-partitioned path (release=YYYY-MM-DD) injects a synthetic `release`
    # column. Stream from the local /tmp/ Parquet (already validated + uploaded).
    logger.info("Reading local Parquet for Lance write: %s", local_parquet)
    reader = con.execute(
        f"SELECT * FROM read_parquet('{local_parquet}')"
    ).to_arrow_reader(batch_size=100_000)

    # Step 7: lance.write_dataset inside lance_commit_lock
    storage_options = _storage_options()
    t_lance = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("Writing Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        write_dur = time.time() - t_lance
        logger.info(
            "Wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

    # Step 8: Three BTREE indices (outside commit lock — indices commit independently)
    for col in ("LICENSE NUMBER", "business_name_normalized", "LICENSE TYPE"):
        logger.info("Building BTREE index on %s ...", col)
        ds.create_scalar_index(col, index_type="BTREE", replace=True)
        logger.info("BTREE on %s: OK", col)

    # Step 9: Compact + cleanup
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_lance - write_dur
    result = {
        "status": "succeeded",
        "release": release,
        "rows_parquet": row_count,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "parquet_uri": parquet_uri,
        "copy_duration_s": round(copy_dur, 1),
        "upload_duration_s": round(upload_dur, 1),
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }
    logger.info("emit complete: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TDLR All-Licenses → Lance emit (one-off)"
    )
    parser.add_argument(
        "--csv-path",
        required=True,
        help="Path to TDLR All Licenses CSV export",
    )
    parser.add_argument(
        "--release",
        required=True,
        help="Release date string, e.g. 2026-05-29",
    )
    args = parser.parse_args()

    if not Path(args.csv_path).exists():
        logger.error("CSV not found: %s", args.csv_path)
        sys.exit(1)

    import json
    out = emit(args.csv_path, args.release)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
