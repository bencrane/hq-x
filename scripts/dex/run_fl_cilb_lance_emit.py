"""FL CILB licensees → Lance emit (Pattern A, Modal-chained daily after s1 ingest).

Reads the latest-release Parquet from R2 and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance

R2 Parquet input:
    s3://dex-raw-landing-zone/fl-cilb/release={release}/data.parquet

Columns:
  - 22 verbatim from CSV (all VARCHAR; set at CSV→Parquet time per L9 "emit verbatim"):
      board_number, occupation_code, licensee_name, doing_business_as_name,
      class_code, address_line_1, address_line_2, address_line_3,
      city, state, zip, county_code, license_number, primary_status,
      secondary_status, original_licensure_date, effective_date, expiration_date,
      blank, renewal_period, alternate_license_number, col_22_unknown
      (Live CSV confirmed 22 cols 2026-05-18; col_23 not present in actual data)
  - licensee_name_normalized (VARCHAR, additive computed column via
    normalize_entity_name DuckDB UDF per L34)

BTREE indices on license_number + licensee_name_normalized (two consecutive
ds.create_scalar_index calls per validator p1 — inline write_dataset pattern,
NOT via the single-btree helper; mirrors PR #480 run_cslb_licensees_lance_emit.py).

Trailing col 22 is captured verbatim per Source ingest invariant
§"TRUE 1:1 column mirror" — col names from Stage 1 probe 2026-05-18.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_fl_cilb_lance_emit.py \\
        --release 2026-05-18

Or as one-shot Modal invocation (bootstrap):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_fl_cilb_lance_emit.py

Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-18-fl-cilb-ingest-and-bridges.md
Precedent: PR #480 run_cslb_licensees_lance_emit.py (dual-BTREE inline pattern).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Resolve scripts/ as a package when run directly (not via installed module).
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import duckdb
import lance

from scripts._lib.entity_name_normalize import normalize_entity_name
from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "fl_cilb_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance"

R2_BUCKET = "dex-raw-landing-zone"
R2_PARQUET_TEMPLATE = "s3://dex-raw-landing-zone/fl-cilb/release={release}/data.parquet"

# Validator: row floor at 250K to accommodate row-count drift + status-filter shrinkage.
MIN_ROW_FLOOR = 250_000

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# R2 helpers (mirror PR #480 / run_cslb_licensees_lance_emit.py)
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
    """Connect DuckDB + install httpfs + create R2 secret.

    Mirror PR #480 lines 94-109 of run_cslb_licensees_lance_emit.py.
    """
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

def emit(release: str) -> dict:
    """Pattern A Lance emit: R2 Parquet → DuckDB normalize → Lance + dual BTREE.

    Steps:
    1. Set LANCE env vars.
    2. Download R2 Parquet to /tmp/ via boto3 (mirrors CSLB precedent — avoids
       DuckDB R2 Hive-partition synthetic column injection + routing issues).
    3. DuckDB connect + register normalize_entity_name UDF.
    4. Read local Parquet; add licensee_name_normalized column; row-count floor check.
    5. lance.write_dataset inside lance_commit_lock.
    6. Two BTREE indices: license_number + licensee_name_normalized.
    7. compact_files + cleanup_old_versions.
    """
    import boto3

    # Step 1: Lance env setup (per CSLB engineering-notes precedent)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    parquet_uri = R2_PARQUET_TEMPLATE.format(release=release)
    local_parquet = f"/tmp/fl-cilb-{release}.parquet"
    logger.info("Input Parquet: %s", parquet_uri)
    logger.info("Output Lance: %s", LANCE_URI)

    # Step 2: Download R2 Parquet to /tmp/ via boto3
    # (Reading directly via DuckDB R2 secret can 404 due to routing differences;
    # mirroring CSLB precedent: read from local /tmp/ after boto3 download.)
    if not Path(local_parquet).exists():
        logger.info("Downloading R2 Parquet to %s ...", local_parquet)
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="us-east-1",
        )
        r2_key = f"fl-cilb/release={release}/data.parquet"
        s3.download_file(R2_BUCKET, r2_key, local_parquet)
        logger.info("Downloaded %d bytes", Path(local_parquet).stat().st_size)
    else:
        logger.info("Local Parquet already present: %s (%d bytes)", local_parquet, Path(local_parquet).stat().st_size)

    # Step 3: DuckDB connect + register UDF
    # null_handling="special" required: normalize_entity_name returns None for
    # generic/sole-prop names per L33 / validator p3. Without "special",
    # DuckDB raises InvalidInputError when the UDF returns NULL.
    con = _connect_duckdb()
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        [duckdb.type("VARCHAR")],
        duckdb.type("VARCHAR"),
        null_handling="special",
    )

    # Step 4: Read local Parquet + add licensee_name_normalized; row-count floor check.
    # Parquet columns are already all VARCHAR (set via all_varchar=TRUE at CSV→Parquet time).
    logger.info("Reading local Parquet + computing licensee_name_normalized ...")
    t0 = time.time()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    logger.info("Parquet row count: %d (floor %d)", row_count, MIN_ROW_FLOOR)
    if row_count < MIN_ROW_FLOOR:
        msg = f"FAIL: Parquet row count {row_count} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        sys.exit(1)

    # Step 5: Arrow reader + lance.write_dataset inside commit lock
    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            *,
            py_normalize_entity("licensee_name") AS licensee_name_normalized
        FROM read_parquet('{local_parquet}')
        """
    ).to_arrow_reader(batch_size=100_000)

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

    # Step 6: Two BTREE indices (outside commit lock — indices commit independently)
    # Per validator p1: NOT using the single-btree emit helper (only supports one btree_column).
    logger.info("Building BTREE index on license_number ...")
    ds.create_scalar_index("license_number", index_type="BTREE", replace=True)
    logger.info("BTREE on license_number: OK")

    logger.info("Building BTREE index on licensee_name_normalized ...")
    ds.create_scalar_index("licensee_name_normalized", index_type="BTREE", replace=True)
    logger.info("BTREE on licensee_name_normalized: OK")

    # Step 7: Compact + cleanup
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_lance - write_dur
    total_dur = time.time() - t0
    result = {
        "status": "succeeded",
        "release": release,
        "rows_parquet": row_count,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "parquet_uri": parquet_uri,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "total_duration_s": round(total_dur, 1),
    }
    logger.info("emit complete: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FL CILB → Lance emit (Pattern A, daily-chained)"
    )
    parser.add_argument(
        "--release",
        default=date.today().isoformat(),
        help="Release date string, e.g. 2026-05-18 (default: today)",
    )
    args = parser.parse_args()

    import json
    out = emit(args.release)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
