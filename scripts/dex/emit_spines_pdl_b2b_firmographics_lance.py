"""Spines — PDL B2B Firmographics Lance emit (Master Spine 26).

Pattern A spine emit: projects `pdl.free_companies_lance` (8,843,189 rows)
into a firmographic-aliased schema with a cleaned corporate_domain join key.

Projection (verbatim from the operator's directive):
    pdl_id,
    pdl_name              AS legal_name_raw,
    legal_name_normalized,
    state                 AS physical_state,
    pdl_locality          AS physical_city,
    pdl_industry          AS industry_classification,
    pdl_size              AS employee_count_bracket,
    pdl_founded           AS year_founded,
    pdl_linkedin_url      AS linkedin_url,
    REGEXP_REPLACE(
        REPLACE(REPLACE(pdl_website, 'http://', ''), 'https://', ''),
        '/.*', ''
    ) AS corporate_domain

Strict DISTINCT across the row matrix.

Lance write discipline (Pattern A canonical):
  - LANCE_BYPASS_SPILLING=true; TMPDIR=/tmp/lance
  - lance_commit_lock("spines_pdl_b2b_firmographics_lance")
  - mode="overwrite"
  - BTREE on pdl_id, legal_name_normalized, corporate_domain
  - On index failure: HARD ABORT with rollback to prior version
    (or full R2 prefix delete on first-time emit)
  - ds.optimize.compact_files()
  - ds.cleanup_old_versions(older_than=timedelta(days=7))

Polaris registration: subprocess to init_polaris_lance_generic.py
  --namespace spines --table pdl_b2b_firmographics_lance --doc "<...>"

Run:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with pyarrow --with duckdb --with boto3 \\
                --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/emit_spines_pdl_b2b_firmographics_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger(__name__)

R2_BUCKET = "dex-raw-landing-zone"
SOURCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/pdl/free_companies_lance"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/pdl_b2b_firmographics_lance"
LANCE_PREFIX = "polaris-warehouse/spines/pdl_b2b_firmographics_lance/"

DATASET_SLUG = "spines_pdl_b2b_firmographics_lance"
POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "pdl_b2b_firmographics_lance"
POLARIS_DOC = (
    "PDL B2B Firmographics — Master Spine 26. Projects pdl.free_companies_lance "
    "into a firmographic-aliased schema (legal_name_raw, legal_name_normalized, "
    "physical_state, physical_city, industry_classification, employee_count_bracket, "
    "year_founded, linkedin_url, corporate_domain) with strict DISTINCT and BTREE "
    "scalar indices on pdl_id, legal_name_normalized, corporate_domain. "
    "corporate_domain is derived from pdl_website with http/https scheme stripping "
    "and trailing path removal to drive cross-source identity joins."
)

INDEX_COLUMNS = ("pdl_id", "legal_name_normalized", "corporate_domain")

TMP_DIR = "/tmp/lance"
TMP_DIR_FREE_GB_FLOOR = 5

PROJECT_SQL = """
SELECT DISTINCT
    pdl_id,
    pdl_name                AS legal_name_raw,
    legal_name_normalized,
    state                   AS physical_state,
    pdl_locality            AS physical_city,
    pdl_industry            AS industry_classification,
    pdl_size                AS employee_count_bracket,
    pdl_founded             AS year_founded,
    pdl_linkedin_url        AS linkedin_url,
    REGEXP_REPLACE(
        REPLACE(REPLACE(pdl_website, 'http://', ''), 'https://', ''),
        '/.*',
        ''
    ) AS corporate_domain
FROM source
"""


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _check_tmp_capacity() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    st = os.statvfs(TMP_DIR)
    free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
    LOG.info("TMPDIR=%s free=%.1f GB", TMP_DIR, free_gb)
    if free_gb < TMP_DIR_FREE_GB_FLOOR:
        raise RuntimeError(
            f"FAIL: {TMP_DIR} free {free_gb:.1f} GB < floor {TMP_DIR_FREE_GB_FLOOR} GB — "
            f"refusing to write; risk of Lance commit failure on R2."
        )


def _delete_r2_prefix(prefix: str) -> int:
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    total = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        objs = page.get("Contents", []) or []
        if not objs:
            continue
        s3.delete_objects(
            Bucket=R2_BUCKET,
            Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
        )
        total += len(objs)
    return total


def _rollback(storage_options: dict, prior_version: int | None) -> None:
    """Restore prior Lance version, OR delete the entire R2 prefix on first emit."""
    import lance

    if prior_version is not None:
        LOG.error("ROLLBACK: checkout+restore prior version %s of %s",
                  prior_version, LANCE_URI)
        ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        ds.checkout_version(prior_version)
        ds.restore()
        LOG.error("ROLLBACK: restored to version %s", prior_version)
        return

    LOG.error("ROLLBACK: first-time emit — deleting R2 prefix %s", LANCE_PREFIX)
    deleted = _delete_r2_prefix(LANCE_PREFIX)
    LOG.error("ROLLBACK: deleted %d objects from %s", deleted, LANCE_PREFIX)


def emit() -> dict:
    """Run Pattern A spine emit. Returns metrics dict."""
    import duckdb
    import lance

    _check_tmp_capacity()
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = TMP_DIR

    storage_options = _storage_options()

    LOG.info("opening source %s ...", SOURCE_URI)
    src_ds = lance.dataset(SOURCE_URI, storage_options=storage_options)
    src_rows = src_ds.count_rows()
    LOG.info("  source rows=%d version=%s", src_rows, src_ds.version)

    t0 = time.time()
    LOG.info("scanning source (10 projected columns) ...")
    src_arrow = src_ds.scanner(
        columns=[
            "pdl_id", "pdl_name", "legal_name_normalized", "state",
            "pdl_locality", "pdl_industry", "pdl_size", "pdl_founded",
            "pdl_linkedin_url", "pdl_website",
        ],
    ).to_table()
    LOG.info("  src_arrow rows=%d (%.1fs)", src_arrow.num_rows, time.time() - t0)

    LOG.info("registering source in DuckDB; tuning for DISTINCT aggregation ...")
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("source", src_arrow)

    LOG.info("executing projection + DISTINCT ...")
    t0 = time.time()
    result_arrow = con.execute(PROJECT_SQL).fetch_arrow_table()
    LOG.info("  produced %d DISTINCT rows (%.1fs)",
             result_arrow.num_rows, time.time() - t0)

    metrics = {
        "source_rows": src_rows,
        "spine_rows": result_arrow.num_rows,
    }

    # Capture prior version (if any) for rollback
    prior_version: int | None = None
    try:
        prior_ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        prior_version = prior_ds.version
        LOG.info("prior dataset present: version=%s rows=%d",
                 prior_version, prior_ds.count_rows())
    except Exception:
        LOG.info("no prior dataset at target — first-time spine emit")

    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        t0 = time.time()
        ds = lance.write_dataset(
            result_arrow,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=1_000_000,
        )
        LOG.info("  wrote %d rows in %.1fs (version=%s)",
                 ds.count_rows(), time.time() - t0, ds.version)
        metrics["lance_rows"] = ds.count_rows()
        metrics["lance_version"] = ds.version

        try:
            for col in INDEX_COLUMNS:
                t_idx = time.time()
                LOG.info("creating BTREE on %s (replace=True) ...", col)
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                LOG.info("  BTREE on %s built in %.1fs", col, time.time() - t_idx)
        except Exception as exc:  # noqa: BLE001
            LOG.error("INDEX FAILED on column — hard abort + rollback: %s", exc)
            _rollback(storage_options, prior_version)
            raise

        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))

        indices = ds.list_indices()
        LOG.info("INDICES (post-write):")
        for idx in indices:
            LOG.info("  %s", idx)
        metrics["indices"] = [i["name"] for i in indices]

    return metrics


def register_polaris() -> None:
    """Idempotent Polaris generic-table registration (creates namespace if missing)."""
    cmd = [
        "python3",
        str(Path(__file__).resolve().parent / "init_polaris_lance_generic.py"),
        "--namespace", POLARIS_NAMESPACE,
        "--table", POLARIS_TABLE,
        "--doc", POLARIS_DOC,
    ]
    LOG.info("calling Polaris registration: %s", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        raise RuntimeError(f"Polaris registration exited rc={rc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Spines — PDL B2B Firmographics Lance emit")
    ap.add_argument(
        "--skip-polaris", action="store_true",
        help="Write Lance dataset only; do not call init_polaris_lance_generic.py",
    )
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    metrics = emit()
    LOG.info("EMIT METRICS: %s", metrics)

    if args.skip_polaris:
        LOG.info("--skip-polaris set; skipping Polaris registration")
    else:
        register_polaris()

    LOG.info("DONE: spine rows=%d uri=%s", metrics["lance_rows"], LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
