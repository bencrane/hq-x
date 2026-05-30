"""Build spines/uspto_patent_lance — master USPTO Granted-Patent substrate.

Pattern A enriched-cohort emit: LEFT JOIN of PatentsView g_patent + g_application
parquet snapshots on R2, projected to patent grain with parsed grant + filing
dates, DISTINCT'd, written to Lance under the spines/ namespace.

Sources (PatentsView Granted bulk, R2 raw landing zone — NOT Lance):
  - s3://dex-raw-landing-zone/uspto-patents/granted/g_patent/*.parquet
        (9,454,161 rows; patent_id + patent_type + patent_title + patent_date)
  - s3://dex-raw-landing-zone/uspto-patents/granted/g_application/*.parquet
        (9,451,902 rows; application_id + patent_id + filing_date)

Output:
  s3://dex-raw-landing-zone/polaris-warehouse/spines/uspto_patent_lance/

Projection (per granted patent):
  patent_number   = pat.patent_id
  patent_type     = pat.patent_type
  title           = pat.patent_title
  grant_date      = try_strptime(pat.patent_date, ['%Y-%m-%d','%m/%d/%Y','%Y%m%d'])::DATE
  application_id  = app.application_id
  filing_date     = try_strptime(app.filing_date, ['%Y-%m-%d','%m/%d/%Y','%Y%m%d'])::DATE

LEFT JOIN preserves patents with no g_application row (NULL application_id +
filing_date). DISTINCT collapses any duplicate projected tuples.

Hard-fail policy:
  - All three BTREE index builds (patent_number, grant_date, filing_date) raise
    to the top — no try/except swallow.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import lance

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("build-uspto-patent-spine")

R2_BUCKET = "dex-raw-landing-zone"
# Raw landing-zone parquet is Hive-partitioned: snapshot=YYYY-MM-DD/data.parquet.
# Match the snapshot directory in the glob so DuckDB resolves the actual file.
PAT_GLOB = f"s3://{R2_BUCKET}/uspto-patents/granted/g_patent/*/*.parquet"
APP_GLOB = f"s3://{R2_BUCKET}/uspto-patents/granted/g_application/*/*.parquet"

DATASET_SLUG = "uspto_patent_lance"
SPINE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/{DATASET_SLUG}"


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _configure_duckdb_for_r2(con: duckdb.DuckDBPyConnection) -> None:
    """Install httpfs + register an S3 secret pointing at the R2 endpoint."""
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").replace("http://", "")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2 (
            TYPE S3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{endpoint}',
            REGION 'us-east-1',
            URL_STYLE 'path',
            USE_SSL true
        );
        """
    )


def main() -> int:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.makedirs("/tmp/lance", exist_ok=True)

    so = _r2_storage_options()

    LOG.info("Configuring DuckDB ...")
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    _configure_duckdb_for_r2(con)

    LOG.info("Running LEFT JOIN + DISTINCT (pat ⟕ app on patent_id) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE spine AS
        SELECT DISTINCT * FROM (
            SELECT
                pat.patent_id                                                       AS patent_number,
                pat.patent_type                                                     AS patent_type,
                pat.patent_title                                                    AS title,
                try_strptime(pat.patent_date, ['%Y-%m-%d','%m/%d/%Y','%Y%m%d'])::DATE AS grant_date,
                app.application_id                                                  AS application_id,
                try_strptime(app.filing_date, ['%Y-%m-%d','%m/%d/%Y','%Y%m%d'])::DATE AS filing_date
            FROM read_parquet('{PAT_GLOB}') AS pat
            LEFT JOIN read_parquet('{APP_GLOB}') AS app
              ON pat.patent_id = app.patent_id
        )
        """
    )
    rows = con.execute("SELECT count(*) FROM spine").fetchone()[0]
    n_with_app = con.execute(
        "SELECT count(*) FROM spine WHERE application_id IS NOT NULL"
    ).fetchone()[0]
    n_unparsed_grant = con.execute(
        "SELECT count(*) FROM spine WHERE grant_date IS NULL"
    ).fetchone()[0]
    n_unparsed_filing = con.execute(
        "SELECT count(*) FROM spine WHERE filing_date IS NULL AND application_id IS NOT NULL"
    ).fetchone()[0]
    LOG.info("  spine rows (post-DISTINCT): %d", rows)
    LOG.info("    with application_id      : %d", n_with_app)
    LOG.info("    grant_date NULL          : %d", n_unparsed_grant)
    LOG.info("    filing_date NULL (w/ app): %d", n_unparsed_filing)

    LOG.info("Writing Lance dataset (mode=overwrite) to %s ...", SPINE_URI)
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query("SELECT * FROM spine").to_arrow_reader(
            batch_size=100_000,
        )
        ds = lance.write_dataset(
            reader,
            SPINE_URI,
            mode="overwrite",
            storage_options=so,
        )
        LOG.info("  write complete: %d rows", ds.count_rows())

        for col in ("patent_number", "grant_date", "filing_date"):
            LOG.info("Building BTREE index on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            LOG.info("  BTREE(%s): OK", col)

        LOG.info("Compacting fragments + cleaning old versions ...")
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))

    LOG.info("DONE: %s rows=%d", SPINE_URI, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
