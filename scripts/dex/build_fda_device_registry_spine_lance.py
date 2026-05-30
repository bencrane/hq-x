"""Build spines/fda_device_registry_lance — unified FDA medical-device registry.

Pattern A enriched-cohort emit: UNION ALL of the openFDA PMA and 510(k) Lance
substrates, projected to a single submission-grain shape, DISTINCT'd, written
to a new Lance dataset under the spines/ namespace.

Sources:
  - s3://.../polaris-warehouse/openfda/device_pma_lance/   (56,340 rows;
        keyed on (pma_number, supplement_number); applicant + trade_name +
        generic_name + product_code + docket_number + decision_date_typed)
  - s3://.../polaris-warehouse/openfda/device_510k_lance/  (174,936 rows;
        keyed on k_number; applicant + device_name + product_code +
        decision_date_typed; no trade_name / generic_name / docket_number)

Output:
  s3://.../polaris-warehouse/spines/fda_device_registry_lance/

Projection (per submission):
  fda_submission_id    = COALESCE(pma.pma_number, k510.k_number)
  fda_submission_type  = 'PMA' | '510K'
  applicant_name       = UPPER(TRIM(applicant))
  device_brand_name    = COALESCE(pma.trade_name, k510.device_name)
  generic_name         = pma.generic_name (NULL for 510K)
  product_code         = COALESCE(pma.product_code, k510.product_code)
  docket_number        = pma.docket_number (NULL for 510K)
  clearance_date       = COALESCE(pma.decision_date_typed, k510.decision_date_typed)

DISTINCT collapses PMA multi-supplement rows that project identically
(supplement_number is NOT projected).

Hard-fail policy:
  - All three BTREE index builds (fda_submission_id, applicant_name,
    clearance_date) raise to the top — no try/except swallow.
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
LOG = logging.getLogger("build-fda-device-registry-spine")

R2_BUCKET = "dex-raw-landing-zone"
PMA_URI = f"s3://{R2_BUCKET}/polaris-warehouse/openfda/device_pma_lance/"
K510_URI = f"s3://{R2_BUCKET}/polaris-warehouse/openfda/device_510k_lance/"

DATASET_SLUG = "fda_device_registry_lance"
SPINE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/{DATASET_SLUG}"


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

    LOG.info("Loading projected Arrow tables via PyLance scanner ...")
    ds_pma = lance.dataset(PMA_URI, storage_options=so)
    arrow_pma = ds_pma.scanner(
        columns=[
            "pma_number",
            "applicant",
            "trade_name",
            "generic_name",
            "product_code",
            "docket_number",
            "decision_date_typed",
        ],
    ).to_table()
    LOG.info("  pma: %d rows", arrow_pma.num_rows)

    ds_k510 = lance.dataset(K510_URI, storage_options=so)
    arrow_k510 = ds_k510.scanner(
        columns=[
            "k_number",
            "applicant",
            "device_name",
            "product_code",
            "decision_date_typed",
        ],
    ).to_table()
    LOG.info("  k510: %d rows", arrow_k510.num_rows)

    LOG.info("Configuring DuckDB ...")
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("pma", arrow_pma)
    con.register("k510", arrow_k510)

    LOG.info("Running UNION ALL + DISTINCT ...")
    con.execute(
        """
        CREATE TEMP TABLE registry AS
        SELECT DISTINCT * FROM (
            SELECT
                pma_number                          AS fda_submission_id,
                'PMA'                               AS fda_submission_type,
                UPPER(TRIM(applicant))              AS applicant_name,
                trade_name                          AS device_brand_name,
                generic_name                        AS generic_name,
                product_code                        AS product_code,
                docket_number                       AS docket_number,
                decision_date_typed                 AS clearance_date
            FROM pma
            UNION ALL
            SELECT
                k_number                            AS fda_submission_id,
                '510K'                              AS fda_submission_type,
                UPPER(TRIM(applicant))              AS applicant_name,
                device_name                         AS device_brand_name,
                CAST(NULL AS VARCHAR)               AS generic_name,
                product_code                        AS product_code,
                CAST(NULL AS VARCHAR)               AS docket_number,
                decision_date_typed                 AS clearance_date
            FROM k510
        )
        """
    )
    rows = con.execute("SELECT count(*) FROM registry").fetchone()[0]
    by_type = con.execute(
        "SELECT fda_submission_type, count(*) FROM registry GROUP BY 1 ORDER BY 1"
    ).fetchall()
    LOG.info("  registry rows (post-DISTINCT): %d", rows)
    for t, c in by_type:
        LOG.info("    %s: %d", t, c)

    LOG.info("Writing Lance dataset (mode=overwrite) to %s ...", SPINE_URI)
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query("SELECT * FROM registry").to_arrow_reader(
            batch_size=100_000,
        )
        ds = lance.write_dataset(
            reader,
            SPINE_URI,
            mode="overwrite",
            storage_options=so,
        )
        LOG.info("  write complete: %d rows", ds.count_rows())

        for col in ("fda_submission_id", "applicant_name", "clearance_date"):
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
