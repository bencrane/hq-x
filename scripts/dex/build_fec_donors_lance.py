#!/usr/bin/env python3
"""Build spines/fec_donors_lance — derived FEC donor rolodex (person grain).

Pattern A, PERSON grain: one row per person_key, aggregated FROM the canonical
transaction spine (spines/fec_individual_contributions_lance). This is the
ergonomic identity surface (rollups for GTM / lookups) — it is NOT the bridge
join axis (bridges go through the transaction spine to preserve per-contribution
fan-out). Mirrors the epiq claims_resolved → creditors rolodex split.

Single source of normalization truth: reads the spine's already-parsed name
components + person_key; does NO re-parsing. Filters to entity_tp='IND'
(individual contributors); non-person filer types stay only in the spine.

Rollups per person_key: latest name components + state, contribution_count,
total/max amount, first/last date, distinct committees, cycles active,
latest employer/occupation/zip5/city, is_recently_active.

Source : s3://dex-raw-landing-zone/polaris-warehouse/spines/fec_individual_contributions_lance
Output : s3://dex-raw-landing-zone/polaris-warehouse/spines/fec_donors_lance

BTREE: person_key, name_last_key, name_first_key, state, zip5_latest,
employer_normalized_latest.

Usage:
  doppler run --project hq-all --config prd -- python3 \\
    scripts/build_fec_donors_lance.py --apply
  ... --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
os.environ.setdefault("TMPDIR", "/tmp/lance")
Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout
)
log = logging.getLogger("build_fec_donors_lance")

R2_BUCKET = "dex-raw-landing-zone"
SPINE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/fec_individual_contributions_lance"
DEFAULT_OUTPUT_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/fec_donors_lance"

POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "fec_donors_lance"

SCAN_COLS = [
    "person_key", "name_first", "name_middle", "name_last", "name_suffix",
    "name_last_key", "name_first_key", "state", "transaction_amt", "transaction_dt",
    "cmte_id", "employer", "employer_normalized", "occupation", "occupation_normalized",
    "zip5", "city", "cycle_year", "entity_tp",
]

BTREE_COLS = (
    "person_key",
    "name_last_key",
    "name_first_key",
    "state",
    "zip5_latest",
    "employer_normalized_latest",
)

AGG_SQL = """
SELECT
    person_key,
    any_value(name_last_key)                              AS name_last_key,
    any_value(name_first_key)                             AS name_first_key,
    arg_max(name_first,  transaction_dt)                  AS name_first,
    arg_max(name_middle, transaction_dt)                  AS name_middle,
    arg_max(name_last,   transaction_dt)                  AS name_last,
    arg_max(name_suffix, transaction_dt)                  AS name_suffix,
    any_value(state)                                      AS state,
    count(*)                                              AS contribution_count,
    sum(transaction_amt)                                  AS total_amount,
    max(transaction_amt)                                  AS max_contribution,
    min(transaction_dt)                                   AS first_contribution_date,
    max(transaction_dt)                                   AS last_contribution_date,
    count(DISTINCT cmte_id)                               AS distinct_committees,
    count(DISTINCT cycle_year)                            AS cycles_active,
    min(cycle_year)                                       AS first_cycle,
    max(cycle_year)                                       AS last_cycle,
    arg_max(employer,             transaction_dt)         AS employer_latest,
    arg_max(employer_normalized,  transaction_dt)         AS employer_normalized_latest,
    arg_max(occupation,           transaction_dt)         AS occupation_latest,
    arg_max(occupation_normalized,transaction_dt)         AS occupation_normalized_latest,
    arg_max(zip5, transaction_dt)                         AS zip5_latest,
    arg_max(city, transaction_dt)                         AS city_latest,
    (max(cycle_year) >= 2022)                             AS is_recently_active
FROM spine
WHERE person_key IS NOT NULL
GROUP BY person_key
"""


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("SET threads=8; SET memory_limit='64GB';")
    con.execute("SET temp_directory='/tmp/lance'; SET max_temp_directory_size='120GB';")
    con.execute("SET preserve_insertion_order=false;")
    return con


def build(*, apply: bool, spine_uri: str, output_uri: str,
          max_rows_per_file: int, skip_polaris: bool, row_floor: int) -> int:
    import lance

    storage_options = _lance_storage_options()
    is_s3 = output_uri.startswith("s3://")
    if not is_s3:
        Path(output_uri).parent.mkdir(parents=True, exist_ok=True)

    spine = lance.dataset(spine_uri, storage_options=storage_options)
    spine_rows = spine.count_rows()
    log.info("spine: %s  rows=%d", spine_uri, spine_rows)
    log.info("output: %s", output_uri)

    if not apply:
        log.info("DRY RUN — will aggregate IND rows to person grain. No write.")
        return 0

    con = _connect_duckdb()
    log.info("scanning spine (entity_tp='IND') → aggregate per person_key ...")
    t = time.time()
    reader = spine.scanner(columns=SCAN_COLS, filter="entity_tp = 'IND'").to_reader()
    con.register("spine", reader)
    result = con.execute(AGG_SQL).fetch_arrow_table()
    donor_rows = result.num_rows
    log.info("  aggregated to %d donors in %.1fs", donor_rows, time.time() - t)

    if donor_rows < row_floor:
        log.error("FAIL: donor rows %d < floor %d", donor_rows, row_floor)
        return 1

    t_w = time.time()
    ds = lance.write_dataset(
        result, output_uri, mode="overwrite",
        max_rows_per_file=max_rows_per_file, storage_options=storage_options,
    )
    log.info("  wrote %d rows in %.1fs (version=%s)", ds.count_rows(), time.time() - t_w, ds.version)

    for col in BTREE_COLS:
        t_i = time.time()
        log.info("building BTREE index on %s ...", col)
        ds.create_scalar_index(col, index_type="BTREE", replace=True)
        log.info("  BTREE(%s): OK in %.1fs", col, time.time() - t_i)

    try:
        log.info("optimize: compact_files + cleanup_old_versions(7d) ...")
        ds.optimize.compact_files()
    except Exception as e:  # noqa: BLE001
        log.warning("  compact_files failed (non-fatal): %s", e)
    try:
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:  # noqa: BLE001
        log.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    if is_s3 and not skip_polaris:
        from scripts._lib.catalog_hooks import register_or_update_polaris

        log.info("registering Polaris generic-table ...")
        register_or_update_polaris(
            namespace=POLARIS_NAMESPACE,
            table_name=POLARIS_TABLE,
            s3_uri=output_uri,
            docstring=(
                "FEC donor rolodex (Pattern A, person grain, PK person_key) — one row per "
                "individual donor aggregated from spines/fec_individual_contributions_lance "
                "(entity_tp='IND'). Latest name/employer/occupation/geo + contribution_count, "
                "total/max amount, first/last date, distinct committees, cycles active, "
                "is_recently_active. Convenience surface; NOT the bridge join axis — bridges "
                "go through the transaction spine to preserve per-contribution detail."
            ),
        )
    else:
        log.info("skipping Polaris registration (skip_polaris=%s, is_s3=%s)", skip_polaris, is_s3)

    log.info("OK — %s: %d donors, %d BTREE indices", POLARIS_TABLE, donor_rows, len(BTREE_COLS))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build FEC donor rolodex (Lance, person grain)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    ap.add_argument("--spine-uri", default=SPINE_URI)
    ap.add_argument("--output-uri", default=DEFAULT_OUTPUT_URI)
    ap.add_argument("--max-rows-per-file", type=int, default=1_000_000)
    ap.add_argument("--skip-polaris", action="store_true")
    ap.add_argument("--row-floor", type=int, default=0)
    # accepted for Modal-app symmetry with the spine builder; unused here
    ap.add_argument("--cycles", default="")
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            log.error("FAIL: %s not set", var)
            return 64

    return build(
        apply=args.apply, spine_uri=args.spine_uri, output_uri=args.output_uri,
        max_rows_per_file=args.max_rows_per_file, skip_polaris=args.skip_polaris,
        row_floor=args.row_floor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
