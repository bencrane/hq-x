"""Chicago Home Repair contractor licenses -> Lance emit (Pattern A, one-off).

Chicago licenses home-repair / residential contractors via BACP. This pulls the
"Home Repair" license slice of the Chicago Business Licenses dataset (Socrata
data.cityofchicago.org / r5kz-chrr) and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/licensure/chicago_home_repair_lance

R2 Parquet intermediate at:
    s3://dex-raw-landing-zone/chicago-home-repair/release={release}/data.parquet

Source (API-filtered): r5kz-chrr where license_description='Home Repair'
(26,142 license records confirmed 2026-05-29). Chicago issues no standalone
general-contractor license in open data; Home Repair is the licensed-contractor
roster (commercial GC activity lives in Building Permits contacts -> separate set).

Columns: 37 verbatim from CSV (all VARCHAR) + business_name_normalized
(additive, normalize_entity_name on legal_name).

BTREE indices on license_number + business_name_normalized + license_status.
Mirrors the other licensure emits.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_chicago_home_repair_lance_emit.py \\
        --csv-path "/tmp/chicago_home_repair.csv" --release 2026-05-29
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

DATASET_SLUG = "chicago_home_repair_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/licensure/chicago_home_repair_lance"
MIN_ROW_FLOOR = 20_000  # probe parsed 26,142 on 2026-05-29

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


def emit(csv_path: str, release: str) -> dict:
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    local_parquet = f"/tmp/chicago-home-repair-{release}.parquet"
    r2_key = f"chicago-home-repair/release={release}/data.parquet"
    r2_bucket = "dex-raw-landing-zone"
    parquet_uri = f"s3://{r2_bucket}/{r2_key}"

    con = _connect_duckdb()
    con.create_function(
        "py_normalize_entity", normalize_entity_name,
        [duckdb.type("VARCHAR")], duckdb.type("VARCHAR"), null_handling="special",
    )

    logger.info("COPY CSV -> local Parquet: %s", local_parquet)
    t0 = time.time()
    con.execute(
        f"""
        COPY (
            SELECT *, py_normalize_entity("legal_name") AS business_name_normalized
            FROM read_csv('{csv_path}', all_varchar=TRUE, header=TRUE,
                          delim=',', quote='"', escape='"', strict_mode=false, ignore_errors=FALSE)
        ) TO '{local_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    copy_dur = time.time() - t0
    logger.info("COPY done in %.1fs", copy_dur)

    row_count = con.execute(f"SELECT count(*) FROM read_parquet('{local_parquet}')").fetchone()[0]
    logger.info("Parquet row count: %d (floor %d)", row_count, MIN_ROW_FLOOR)
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

    for col in ("license_number", "business_name_normalized", "license_status"):
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
        "status": "succeeded", "release": release, "rows_parquet": row_count,
        "rows_lance": lance_count, "lance_uri": LANCE_URI, "parquet_uri": parquet_uri,
        "copy_duration_s": round(copy_dur, 1), "upload_duration_s": round(upload_dur, 1),
        "write_duration_s": round(write_dur, 1), "btree_duration_s": round(btree_dur, 1),
    }
    logger.info("emit complete: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Chicago Home Repair licenses -> Lance emit (one-off)")
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
