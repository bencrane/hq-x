"""Colorado SoS (CDOS) Business Entities + Trade Names -> R2 ZSTD Parquet.

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Step 1 of the CO SoS ingest cycle: land a TRUE 1:1 raw mirror of two
operator-staged Socrata CSV exports from the Colorado Information Marketplace
(data.colorado.gov) into Cloudflare R2 as ZSTD Parquet.

  - Business Entities in Colorado    (Socrata 4ykn-tg5h; 3,049,389 rows; 35 cols)
  - Trade Names for Businesses in CO (Socrata u7sb-g482;   285,048 rows; 25 cols)

All columns are pinned VARCHAR at the read_csv step (L9). This is a raw mirror
only -- entity-name normalization and the entitystatus-suffix strip happen in
the step-2 Lance emit ("land, then process"). One-shot: no refresh cadence and
no ingest-runs ledger (operator decision 2026-05-21).

R2 layout (mirrors sos-ca / sos-ny / sos-fl):
  s3://dex-raw-landing-zone/sos-co/release=2026-05-21/entities/data.parquet
  s3://dex-raw-landing-zone/sos-co/release=2026-05-21/trade_names/data.parquet

L42: boto3 upload sets ExtraArgs={'ContentType': 'application/x-parquet'} only;
plain .parquet key suffix, no ContentEncoding.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- uv run python \\
        scripts/run_co_sos_to_r2.py --apply \\
        --entities-csv    /path/to/Business_Entities_in_Colorado_YYYYMMDD.csv \\
        --trade-names-csv /path/to/Trade_Names_for_Businesses_in_Colorado_YYYYMMDD.csv
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import boto3
import duckdb

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# -- load-bearing constants --------------------------------------------------

R2_BUCKET = "dex-raw-landing-zone"
RELEASE = "2026-05-21"
R2_PREFIX = f"sos-co/release={RELEASE}"
TMP_DIR = "/tmp/co-sos"

# Business Entities: CSV header is already the lowercase Socrata fieldName set
# (entityid, entityname, ...). Pass through 1:1.
ENTITIES_SELECT = "*"

# Trade Names: the Socrata CSV export header is camelCase plus one
# space-separated column ("Entity ID"). Project to the lowercase Socrata
# fieldName convention so the Parquet column names are clean AND `entityid`
# matches the Business Entities join key. 25 columns, 1:1.
TRADE_NAMES_SELECT = """
    "masterTradenameId"      AS mastertradenameid,
    "tradenameDescription"   AS tradenamedescription,
    "tradenameForm"          AS tradenameform,
    "effectiveDate"          AS effectivedate,
    "firstName"              AS firstname,
    "middleName"             AS middlename,
    "lastName"               AS lastname,
    "suffix"                 AS suffix,
    "registrantOrganization" AS registrantorganization,
    "address1"               AS address1,
    "address2"               AS address2,
    "city"                   AS city,
    "state"                  AS state,
    "zipCode"                AS zipcode,
    "country"                AS country,
    "mailingAddress1"        AS mailingaddress1,
    "mailingAddress2"        AS mailingaddress2,
    "mailingCity"            AS mailingcity,
    "mailingState"           AS mailingstate,
    "mailingZipCode"         AS mailingzipcode,
    "mailingCountry"         AS mailingcountry,
    "dateAdded"              AS dateadded,
    "entityStatus"           AS entitystatus,
    "entityFormDate"         AS entityformdate,
    "Entity ID"              AS entityid
"""

EXPECTED_COLS = {"entities": 35, "trade_names": 25}


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _duckdb_with_r2() -> duckdb.DuckDBPyConnection:
    """DuckDB connection with httpfs + an R2 secret (for read-back verify)."""
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
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


def transcode_one(
    con: duckdb.DuckDBPyConnection,
    table: str,
    csv_path: str,
    select_sql: str,
    apply: bool,
) -> dict:
    """CSV -> ZSTD Parquet -> R2 for one table. Returns a metrics dict."""
    t0 = time.time()
    logger.info("=" * 64)
    logger.info("[%s] source CSV: %s", table, csv_path)
    if not Path(csv_path).is_file():
        raise SystemExit(f"FAIL: CSV not found: {csv_path}")

    # Load CSV -> in-memory table (single scan). all_varchar per L9.
    # Both CSVs verified RFC4180-clean (every row has exactly 35 / 25 fields),
    # so a plain header read is correct. null_padding/strict_mode=FALSE are
    # omitted deliberately: they made DuckDB's lenient sniffer over-widen
    # entities to 37 columns. They belong on headerless drift-prone CSVs (L56),
    # not a clean headered Socrata export.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE src AS
        SELECT {select_sql}
        FROM read_csv(
            '{csv_path}',
            all_varchar=TRUE,
            header=TRUE
        )
        """
    )
    csv_rows = con.execute("SELECT count(*) FROM src").fetchone()[0]
    n_cols = len(con.execute("DESCRIBE src").fetchall())
    logger.info("[%s] loaded %d rows, %d columns", table, csv_rows, n_cols)

    exp_cols = EXPECTED_COLS[table]
    if n_cols != exp_cols:
        raise SystemExit(
            f"FAIL [{table}]: column count {n_cols} != expected {exp_cols}"
        )

    local_parquet = f"{TMP_DIR}/{table}.parquet"
    con.execute(
        f"""
        COPY src TO '{local_parquet}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    pq_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    pq_bytes = Path(local_parquet).stat().st_size
    logger.info(
        "[%s] wrote %s -- %d rows, %.1f MB ZSTD Parquet",
        table, local_parquet, pq_rows, pq_bytes / 1024 / 1024,
    )

    # L41 parity gate: Parquet row count must equal source CSV row count.
    if pq_rows != csv_rows:
        raise SystemExit(
            f"FAIL [{table}]: parity -- parquet {pq_rows} != csv {csv_rows}"
        )

    r2_key = f"{R2_PREFIX}/{table}/data.parquet"
    if not apply:
        logger.info(
            "[%s] DRY RUN -- skip upload to s3://%s/%s", table, R2_BUCKET, r2_key
        )
        return {
            "table": table, "rows": pq_rows, "cols": n_cols,
            "parquet_bytes": pq_bytes, "r2_key": r2_key, "uploaded": False,
        }

    # L42: ContentType only -- no ContentEncoding; plain .parquet key.
    s3 = _r2_client()
    s3.upload_file(
        local_parquet, R2_BUCKET, r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    head = s3.head_object(Bucket=R2_BUCKET, Key=r2_key)
    logger.info(
        "[%s] uploaded -> s3://%s/%s (%d bytes)",
        table, R2_BUCKET, r2_key, head["ContentLength"],
    )

    # Read-back verify: the R2 Parquet must be readable and row-count must match.
    r2_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('r2://{R2_BUCKET}/{r2_key}')"
    ).fetchone()[0]
    if r2_rows != pq_rows:
        raise SystemExit(
            f"FAIL [{table}]: R2 read-back {r2_rows} != local {pq_rows}"
        )
    logger.info(
        "[%s] R2 read-back verified: %d rows  (%.1fs)",
        table, r2_rows, time.time() - t0,
    )
    return {
        "table": table, "rows": r2_rows, "cols": n_cols,
        "parquet_bytes": pq_bytes, "r2_key": r2_key, "uploaded": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CO SoS Business Entities + Trade Names -> R2 (step 1)"
    )
    ap.add_argument("--entities-csv", required=True, help="Business Entities CSV path")
    ap.add_argument("--trade-names-csv", required=True, help="Trade Names CSV path")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="upload to R2")
    grp.add_argument("--dry-run", action="store_true", help="transcode + verify only")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = _duckdb_with_r2()

    results = [
        transcode_one(con, "entities", args.entities_csv, ENTITIES_SELECT, args.apply),
        transcode_one(
            con, "trade_names", args.trade_names_csv, TRADE_NAMES_SELECT, args.apply
        ),
    ]

    logger.info("=" * 64)
    for r in results:
        logger.info(
            "DONE %s: %d rows, %d cols, %.1f MB -> %s%s",
            r["table"], r["rows"], r["cols"], r["parquet_bytes"] / 1024 / 1024,
            r["r2_key"], "" if r["uploaded"] else "  (dry-run, not uploaded)",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
