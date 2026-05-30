"""Chicago Building-Permit Contractors -> Lance emit (Pattern A w/ contact explode).

Chicago issues no standalone general-contractor license in open data; the GC and
trade-contractor signal lives in the Building Permits dataset (Socrata
data.cityofchicago.org / ydr8-5enu, 837,049 permits) as up to 15 per-permit
"contacts" (CONTACT_1..15_TYPE/NAME/CITY/STATE/ZIPCODE). This emit UNPIVOTS those
15 contact slots into one row per (permit, contractor-contact), filtered to
contractor contact types (CONTACT_n_TYPE ILIKE '%CONTRACTOR%' -- captures
CONTRACTOR-GENERAL CONTRACTOR, OWNER AS GENERAL CONTRACTOR, ELECTRICAL/MASONRY/
PLUMBING/ELEVATOR/SIGN/WRECKING CONTRACTOR, etc.), carrying project context
(type, work, reported cost, issue date, address, geo).

Writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/chicago/permit_contractors_lance
R2 Parquet intermediate at:
    s3://dex-raw-landing-zone/chicago-permit-contractors/release={release}/data.parquet

Input: full Building Permits export (rows.csv), UPPERCASE_UNDERSCORE header.

Output columns (contractor grain):
    permit_number, permit_type, work_type, work_description, issue_date,
    reported_cost, street_number, street_direction, street_name, community_area,
    ward, latitude, longitude, contact_slot, contact_type, contractor_name,
    contractor_city, contractor_state, contractor_zipcode, business_name_normalized

BTREE on business_name_normalized + contact_type + permit_number. A clean GC
roster is then SELECT DISTINCT contractor_name WHERE contact_type ILIKE
'%GENERAL CONTRACTOR%' (or rank by permit count / total reported_cost).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_chicago_permit_contractors_lance_emit.py \\
        --csv-path "/tmp/chicago_building_permits.csv" --release 2026-05-29
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import boto3
import duckdb
import lance

from scripts._lib.entity_name_normalize import normalize_entity_name
from scripts._lib.lance_commit_lock import lance_commit_lock

DATASET_SLUG = "chicago_permit_contractors_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/chicago/permit_contractors_lance"
MIN_ROW_FLOOR = 500_000  # ~540K permits carry a contractor in contact_1 alone; explode >1M

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)


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
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET memory_limit='8GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='80GB'")
    con.execute("SET preserve_insertion_order=false")
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


def _explode_sql() -> str:
    """UNION ALL across the 15 contact slots, contractor contacts only."""
    blocks = []
    for n in range(1, 16):
        blocks.append(
            f"""
            SELECT
                "PERMIT#"             AS permit_number,
                PERMIT_TYPE           AS permit_type,
                WORK_TYPE             AS work_type,
                WORK_DESCRIPTION      AS work_description,
                ISSUE_DATE            AS issue_date,
                REPORTED_COST         AS reported_cost,
                STREET_NUMBER         AS street_number,
                STREET_DIRECTION      AS street_direction,
                STREET_NAME           AS street_name,
                COMMUNITY_AREA        AS community_area,
                WARD                  AS ward,
                LATITUDE              AS latitude,
                LONGITUDE             AS longitude,
                {n}                   AS contact_slot,
                CONTACT_{n}_TYPE      AS contact_type,
                CONTACT_{n}_NAME      AS contractor_name,
                CONTACT_{n}_CITY      AS contractor_city,
                CONTACT_{n}_STATE     AS contractor_state,
                CONTACT_{n}_ZIPCODE   AS contractor_zipcode
            FROM permits
            WHERE CONTACT_{n}_TYPE ILIKE '%CONTRACTOR%'
              AND CONTACT_{n}_NAME IS NOT NULL AND CONTACT_{n}_NAME <> ''
            """
        )
    return "\nUNION ALL\n".join(blocks)


def emit(csv_path: str, release: str) -> dict:
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    local_parquet = f"/tmp/chicago-permit-contractors-{release}.parquet"
    r2_key = f"chicago-permit-contractors/release={release}/data.parquet"
    r2_bucket = "dex-raw-landing-zone"
    parquet_uri = f"s3://{r2_bucket}/{r2_key}"

    con = _connect_duckdb()
    con.create_function(
        "py_normalize_entity", normalize_entity_name,
        [duckdb.type("VARCHAR")], duckdb.type("VARCHAR"), null_handling="special",
    )

    logger.info("Loading permits CSV into DuckDB: %s", csv_path)
    t0 = time.time()
    con.execute(
        f"""
        CREATE TABLE permits AS
        SELECT * FROM read_csv('{csv_path}', all_varchar=TRUE, header=TRUE,
                               delim=',', quote='"', escape='"', strict_mode=false, ignore_errors=FALSE)
        """
    )
    n_permits = con.execute("SELECT count(*) FROM permits").fetchone()[0]
    logger.info("Loaded %d permits in %.1fs", n_permits, time.time() - t0)

    logger.info("Exploding 15 contact slots -> contractor grain + normalize ...")
    t1 = time.time()
    con.execute(
        f"""
        COPY (
            SELECT *, py_normalize_entity(contractor_name) AS business_name_normalized
            FROM ( {_explode_sql()} )
        ) TO '{local_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    copy_dur = time.time() - t1
    logger.info("Explode + COPY done in %.1fs", copy_dur)

    row_count = con.execute(f"SELECT count(*) FROM read_parquet('{local_parquet}')").fetchone()[0]
    logger.info("Contractor-grain row count: %d (floor %d)", row_count, MIN_ROW_FLOOR)
    if row_count < MIN_ROW_FLOOR:
        logger.error("FAIL: row count %d below floor %d", row_count, MIN_ROW_FLOOR)
        sys.exit(1)

    logger.info("Uploading to R2: s3://%s/%s", r2_bucket, r2_key)
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="us-east-1",
    )
    t_upload = time.time()
    s3.upload_file(local_parquet, r2_bucket, r2_key, ExtraArgs={"ContentType": "application/x-parquet"})
    upload_dur = time.time() - t_upload
    logger.info("R2 upload done in %.1fs", upload_dur)

    reader = con.execute(f"SELECT * FROM read_parquet('{local_parquet}')").to_arrow_reader(batch_size=100_000)

    storage_options = _storage_options()
    t_lance = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("Writing Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(reader, LANCE_URI, mode="overwrite", storage_options=storage_options)
        lance_count = ds.count_rows()
        write_dur = time.time() - t_lance
        logger.info("Wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

    for col in ("business_name_normalized", "contact_type", "permit_number"):
        logger.info("Building BTREE index on %s ...", col)
        ds.create_scalar_index(col, index_type="BTREE", replace=True)
        logger.info("BTREE on %s: OK", col)

    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_lance - write_dur
    result = {
        "status": "succeeded", "release": release, "permits_loaded": n_permits,
        "rows_contractor_grain": row_count, "rows_lance": lance_count,
        "lance_uri": LANCE_URI, "parquet_uri": parquet_uri,
        "explode_copy_s": round(copy_dur, 1), "upload_s": round(upload_dur, 1),
        "write_s": round(write_dur, 1), "btree_s": round(btree_dur, 1),
    }
    logger.info("emit complete: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Chicago Building-Permit Contractors -> Lance emit")
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    if not Path(args.csv_path).exists():
        logger.error("CSV not found: %s", args.csv_path)
        sys.exit(1)
    import json
    print(json.dumps(emit(args.csv_path, args.release), indent=2, default=str))


if __name__ == "__main__":
    main()
