"""Spines — SEC Investment Adviser Registry Lance emit (Pattern A enriched-cohort).

LEFT JOIN of sec_adv.base_a_lance × sec_adv.part_2_brochures_lance on
crd_number, DISTINCT-by-crd_number (latest filing wins), emitted to
spines.sec_adv_registry_lance.

Projection (verbatim from the operator's directive):
    crd_number, sec_number, legal_name,
    primary_business_name AS dba_name,
    COALESCE(item_4_total_aum_usd,
             TRY_CAST(json_extract_string(raw_json,
                 '$."Total Regulatory Assets Under Management"') AS DOUBLE)
            ) AS total_raum,
    COALESCE(item_4_discretionary_aum_usd,
             TRY_CAST(json_extract_string(raw_json,
                 '$."Discretionary Regulatory Assets Under Management"') AS DOUBLE)
            ) AS discretionary_aum,
    item_5_minimum_account_size_usd AS min_account_size,
    item_4_firm_founded_year AS founded_year

Lance write discipline (Pattern A canonical):
  - LANCE_BYPASS_SPILLING=true; TMPDIR=/tmp/lance
  - lance_commit_lock("spines_sec_adv_registry_lance")
  - mode="overwrite"
  - BTREE on crd_number, legal_name, total_raum
  - ds.optimize.compact_files()
  - ds.cleanup_old_versions(older_than=timedelta(days=7))

Polaris registration: subprocess to init_polaris_lance_generic.py
  --namespace spines --table sec_adv_registry_lance --doc "<...>"

Run:
    cd ~/hq-all/.claude/worktrees/serene-noether-d03205 && \\
        doppler run --project hq-all --config prd -- uv run python \\
        apps/data-engine-x/scripts/emit_spines_sec_adv_registry_lance.py
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
BASE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/base_a_lance"
BROCHURE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/part_2_brochures_lance"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/sec_adv_registry_lance"

DATASET_SLUG = "spines_sec_adv_registry_lance"
POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "sec_adv_registry_lance"
POLARIS_DOC = (
    "SEC Investment Adviser Registry — Form ADV Part 1A firm identity "
    "(crd_number, sec_number, legal_name, dba_name) LEFT-JOINed with typed "
    "Part 2A AUM + fee metrics (total_raum, discretionary_aum, min_account_size, "
    "founded_year). 1 row per crd_number, latest filing wins. "
    "Source: sec_adv.base_a_lance × sec_adv.part_2_brochures_lance."
)

TMP_DIR = "/tmp/lance"
TMP_DIR_FREE_GB_FLOOR = 5  # bail before write if /tmp/lance has <5 GB free


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


_SELECT_SQL = """
WITH joined AS (
    SELECT
        base.crd_number,
        base.sec_number,
        base.legal_name,
        base.primary_business_name AS dba_name,
        COALESCE(
            brochure.item_4_total_aum_usd,
            TRY_CAST(
                json_extract_string(base.raw_json,
                    '$."Total Regulatory Assets Under Management"'
                ) AS DOUBLE
            )
        ) AS total_raum,
        COALESCE(
            brochure.item_4_discretionary_aum_usd,
            TRY_CAST(
                json_extract_string(base.raw_json,
                    '$."Discretionary Regulatory Assets Under Management"'
                ) AS DOUBLE
            )
        ) AS discretionary_aum,
        brochure.item_5_minimum_account_size_usd AS min_account_size,
        brochure.item_4_firm_founded_year AS founded_year,
        base.date_submitted,
        brochure.filing_date AS brochure_filing_date
    FROM base
    LEFT JOIN brochure
      ON base.crd_number = brochure.crd_number
    WHERE base.crd_number IS NOT NULL
)
SELECT
    crd_number,
    sec_number,
    legal_name,
    dba_name,
    total_raum,
    discretionary_aum,
    min_account_size,
    founded_year
FROM joined
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY crd_number
    ORDER BY date_submitted DESC NULLS LAST,
             brochure_filing_date DESC NULLS LAST
) = 1
"""


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


def emit() -> dict:
    """Run Pattern A enriched-cohort emit. Returns metrics dict."""
    import duckdb
    import lance

    _check_tmp_capacity()
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = TMP_DIR

    storage_options = _storage_options()

    # PyLance pre-projection: pull only the columns we need from each side.
    LOG.info("opening source Lance datasets ...")
    ds_base = lance.dataset(BASE_URI, storage_options=storage_options)
    ds_brochure = lance.dataset(BROCHURE_URI, storage_options=storage_options)
    LOG.info("  base.rows=%d  brochure.rows=%d", ds_base.count_rows(), ds_brochure.count_rows())

    t0 = time.time()
    LOG.info("scanning base_a (crd, sec_number, legal_name, primary_business_name, raw_json, date_submitted) ...")
    base_arrow = ds_base.scanner(
        columns=[
            "crd_number",
            "sec_number",
            "legal_name",
            "primary_business_name",
            "raw_json",
            "date_submitted",
        ],
    ).to_table()
    LOG.info("  base_arrow rows=%d  (%.1fs)", base_arrow.num_rows, time.time() - t0)

    t0 = time.time()
    LOG.info("scanning part_2_brochures (crd, item_4_total_aum_usd, item_4_discretionary_aum_usd, "
             "item_5_minimum_account_size_usd, item_4_firm_founded_year, filing_date) ...")
    brochure_arrow = ds_brochure.scanner(
        columns=[
            "crd_number",
            "item_4_total_aum_usd",
            "item_4_discretionary_aum_usd",
            "item_5_minimum_account_size_usd",
            "item_4_firm_founded_year",
            "filing_date",
        ],
    ).to_table()
    LOG.info("  brochure_arrow rows=%d  (%.1fs)", brochure_arrow.num_rows, time.time() - t0)

    LOG.info("registering Arrow tables in DuckDB; tuning for join ...")
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("base", base_arrow)
    con.register("brochure", brochure_arrow)

    LOG.info("executing LEFT JOIN + QUALIFY ROW_NUMBER dedup ...")
    t0 = time.time()
    result_arrow = con.execute(_SELECT_SQL).fetch_arrow_table()
    LOG.info("  produced %d rows (%.1fs)", result_arrow.num_rows, time.time() - t0)

    metrics = {
        "base_rows": base_arrow.num_rows,
        "brochure_rows": brochure_arrow.num_rows,
        "registry_rows": result_arrow.num_rows,
    }

    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        t0 = time.time()
        ds = lance.write_dataset(
            result_arrow,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=50_000,
        )
        LOG.info("  wrote %d rows in %.1fs (version=%s)", ds.count_rows(), time.time() - t0, ds.version)
        metrics["lance_rows"] = ds.count_rows()
        metrics["lance_version"] = ds.version

        for col in ("crd_number", "legal_name", "total_raum"):
            t_idx = time.time()
            LOG.info("creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            LOG.info("  done in %.1fs", time.time() - t_idx)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("optimize/cleanup non-fatal failure: %s", exc)

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
    ap = argparse.ArgumentParser(description="Spines — SEC Investment Adviser Registry Lance emit")
    ap.add_argument(
        "--skip-polaris", action="store_true",
        help="Write Lance dataset only; do not call init_polaris_lance_generic.py",
    )
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    metrics = emit()
    LOG.info("EMIT METRICS: %s", metrics)

    if args.skip_polaris:
        LOG.info("--skip-polaris set; skipping Polaris registration")
    else:
        register_polaris()

    LOG.info("DONE: registry rows=%d uri=%s", metrics["lance_rows"], LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
