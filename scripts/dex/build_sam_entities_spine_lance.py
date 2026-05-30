"""Build spines/sam_entities_lance — SAM.gov authorized corporate entity spine.

Pattern A enriched-cohort emit: single-source projection + status normalization
+ date parsing + DISTINCT over sam_gov/entities_lance. Not a new identity
bridge (no cross-source match logic), so no ops.bridges /
ops.match_method_versions registration.

Source:
  s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance/  (884,203 rows)

Output:
  s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance/

Hard fail policy:
  - All three BTREE index builds (uei, legal_business_name_normalized,
    corporate_website) raise to the top — no try/except swallow. If any
    index fails, the script aborts with non-zero exit.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import lance
import pyarrow.compute as pc

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("build-sam-entities-spine")

R2_BUCKET = "dex-raw-landing-zone"
SRC_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sam_gov/entities_lance/"

DATASET_SLUG = "sam_entities_lance"
SPINE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/{DATASET_SLUG}"

SRC_COLUMNS = [
    "unique_entity_id",
    "uei_normalized",
    "cage_code",
    "legal_business_name",
    "legal_business_name_normalized",
    "dba_name",
    "dba_name_normalized",
    "entity_url",
    "sam_extract_code",
    "registration_expiration_date",
    "initial_registration_date",
    "activation_date",
]


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def main() -> int:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.makedirs("/tmp/lance", exist_ok=True)

    so = _r2_storage_options()

    LOG.info("Loading projected Arrow table via PyLance scanner ...")
    ds_src = lance.dataset(SRC_URI, storage_options=so)
    arrow_src = ds_src.scanner(
        columns=SRC_COLUMNS,
        filter=(
            pc.field("unique_entity_id").is_valid()
            & pc.field("legal_business_name").is_valid()
        ),
    ).to_table()
    LOG.info(
        "  src (entities WHERE uei IS NOT NULL AND legal_business_name IS NOT NULL): %d rows",
        arrow_src.num_rows,
    )

    LOG.info("Configuring DuckDB ...")
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("src", arrow_src)

    LOG.info("Projecting + normalizing + DISTINCT ...")
    con.execute(
        """
        CREATE TEMP TABLE spine AS
        SELECT DISTINCT
            unique_entity_id                                          AS uei,
            uei_normalized,
            cage_code,
            legal_business_name,
            legal_business_name_normalized,
            dba_name,
            dba_name_normalized,
            entity_url                                                AS corporate_website,
            CASE WHEN sam_extract_code = 'A' THEN 'ACTIVE'
                 ELSE 'INACTIVE_OR_OTHER' END                         AS normalized_registration_status,
            try_strptime(registration_expiration_date,
                ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE      AS expiration_date,
            try_strptime(initial_registration_date,
                ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE      AS initial_registration_date,
            try_strptime(activation_date,
                ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE      AS activation_date
        FROM src
        WHERE unique_entity_id IS NOT NULL
          AND legal_business_name IS NOT NULL
        """
    )
    rows = con.execute("SELECT count(*) FROM spine").fetchone()[0]
    LOG.info("  spine rows (post-DISTINCT): %d", rows)

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

        # HARD FAIL on any index failure — no try/except swallow.
        for col in ("uei", "legal_business_name_normalized", "corporate_website"):
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
