"""Add Tier 3 typed columns to sec_adv.part_2_brochures_lance.

Re-emits the Lance dataset with new typed columns merged in via DuckDB LEFT JOIN
on (crd_number, version_id). Rows present in stage_c_<item>.parquet get the typed
values; rows absent get NULLs for the new columns. The original Tier 1+2 columns
are preserved verbatim.

Versioning: Lance keeps the prior version available for 7 days (see cleanup_old_versions
in emit_sec_adv_part_2_brochures_lance.py) — if anything's wrong post-add, `ds.restore(<prior version>)`
rolls back atomically.

Polaris registration is unchanged (same namespace/table/format/base-location).

Run (Item 5):
    cd /Users/benjamincrane/hq-all && doppler run --project hq-all --config prd -- \\
        apps/data-engine-x/.venv/bin/python3 \\
        apps/data-engine-x/scripts/add_tier3_columns_to_brochures_lance.py \\
        --item 5 \\
        --stage-c-key sec-iapd/form-adv-part-2-extracted/release=2026-05-18/stage_c_item5_part2a_full.parquet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
)
LOG = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/part_2_brochures_lance/"

# Per-item column manifests — columns added to the brochures Lance by this script.
ITEM_COLUMNS: dict[int, list[tuple[str, str]]] = {
    5: [
        ("item_5_extracted", "BOOLEAN"),
        ("item_5_fee_structure_types", "VARCHAR"),       # JSON array string
        ("item_5_aum_tier_breakpoints", "VARCHAR"),      # JSON array string
        ("item_5_performance_fee_pct", "DOUBLE"),
        ("item_5_performance_fee_hurdle_rate_pct", "DOUBLE"),
        ("item_5_performance_fee_high_water_mark", "BOOLEAN"),
        ("item_5_minimum_account_size_usd", "DOUBLE"),
        ("item_5_fees_negotiable", "BOOLEAN"),
        ("item_5_commissions_received", "BOOLEAN"),
        ("item_5_wrap_fee_program", "BOOLEAN"),
        ("item_5_aum_referenced_usd", "DOUBLE"),
        ("item_5_extraction_confidence", "VARCHAR"),
        ("item_5_extraction_notes", "VARCHAR"),
        ("item_5_extractor_version", "VARCHAR"),
    ],
    4: [
        ("item_4_extracted", "BOOLEAN"),
        ("item_4_total_aum_usd", "DOUBLE"),
        ("item_4_discretionary_aum_usd", "DOUBLE"),
        ("item_4_non_discretionary_aum_usd", "DOUBLE"),
        ("item_4_aum_as_of_date", "VARCHAR"),            # ISO date string
        ("item_4_firm_founded_year", "BIGINT"),
        ("item_4_services_offered", "VARCHAR"),          # JSON array string
        ("item_4_tailored_to_individual_clients", "BOOLEAN"),
        ("item_4_wrap_fee_sponsor", "BOOLEAN"),
        ("item_4_aum_extraction_confidence", "VARCHAR"),
        ("item_4_aum_extraction_notes", "VARCHAR"),
        ("item_4_extractor_version", "VARCHAR"),
    ],
}

# Stage_c parquet -> Lance column rename map (stage_c uses unprefixed names so the
# Pydantic schema stays clean; Lance prefixes with item_N_ to namespace among items)
ITEM_RENAME: dict[int, dict[str, str]] = {
    5: {
        "item_5_extracted": "item_5_extracted",
        "fee_structure_types": "item_5_fee_structure_types",
        "aum_tier_breakpoints": "item_5_aum_tier_breakpoints",
        "performance_fee_pct": "item_5_performance_fee_pct",
        "performance_fee_hurdle_rate_pct": "item_5_performance_fee_hurdle_rate_pct",
        "performance_fee_high_water_mark": "item_5_performance_fee_high_water_mark",
        "minimum_account_size_usd": "item_5_minimum_account_size_usd",
        "fees_negotiable": "item_5_fees_negotiable",
        "commissions_received": "item_5_commissions_received",
        "wrap_fee_program": "item_5_wrap_fee_program",
        "aum_referenced_in_item_5_usd": "item_5_aum_referenced_usd",
        "fee_extraction_confidence": "item_5_extraction_confidence",
        "fee_extraction_notes": "item_5_extraction_notes",
        "extractor_version": "item_5_extractor_version",
    },
    4: {
        "item_4_extracted": "item_4_extracted",
        "total_aum_usd": "item_4_total_aum_usd",
        "discretionary_aum_usd": "item_4_discretionary_aum_usd",
        "non_discretionary_aum_usd": "item_4_non_discretionary_aum_usd",
        "aum_as_of_date": "item_4_aum_as_of_date",
        "firm_founded_year": "item_4_firm_founded_year",
        "services_offered": "item_4_services_offered",
        "tailored_to_individual_clients": "item_4_tailored_to_individual_clients",
        "wrap_fee_sponsor": "item_4_wrap_fee_sponsor",
        "aum_extraction_confidence": "item_4_aum_extraction_confidence",
        "aum_extraction_notes": "item_4_aum_extraction_notes",
        "extractor_version": "item_4_extractor_version",
    },
}


def _r2_storage_options() -> dict:
    return {
        "endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
    }


def _r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def add_item_columns(item: int, stage_c_key: str) -> dict:
    import duckdb, lance

    if item not in ITEM_COLUMNS:
        raise ValueError(f"unsupported item={item}; supported: {sorted(ITEM_COLUMNS)}")

    t0 = time.time()
    s3 = _r2_client()
    local_stage_c = f"/tmp/sec_iapd_tier3_item{item}_addcols.parquet"
    LOG.info("downloading s3://%s/%s -> %s", R2_BUCKET, stage_c_key, local_stage_c)
    s3.download_file(R2_BUCKET, stage_c_key, local_stage_c)

    # Read existing Lance dataset (full table)
    ds = lance.dataset(LANCE_URI, storage_options=_r2_storage_options())
    base_version = ds.version
    base_rows = ds.count_rows()
    LOG.info("base Lance: version=%d rows=%d", base_version, base_rows)
    base_tbl = ds.to_table()

    # DuckDB LEFT JOIN base on stage_c, projecting renamed item_N_* columns
    con = duckdb.connect()
    con.register("base", base_tbl)
    con.execute(f"CREATE OR REPLACE VIEW stg AS SELECT * FROM read_parquet('{local_stage_c}')")

    rename = ITEM_RENAME[item]
    stage_c_select_parts = []
    for src, dst in rename.items():
        if src == dst:
            stage_c_select_parts.append(f"stg.{src}")
        else:
            stage_c_select_parts.append(f"stg.{src} AS {dst}")
    stage_c_cols_sql = ",\n            ".join(stage_c_select_parts)

    # Re-run safety: if this item's columns already exist on base (a prior
    # add_item_columns run for the same item — e.g. a partial-corpus extraction
    # being superseded by a fuller one), EXCLUDE them from base.* so the fresh
    # stage_c projection replaces rather than collides on a duplicate column
    # name. First-time add for an item: cols_to_exclude is empty, base_select
    # stays "base.*" — no behaviour change.
    item_col_names = [name for name, _ in ITEM_COLUMNS[item]]
    cols_to_exclude = [c for c in item_col_names if c in base_tbl.column_names]
    if cols_to_exclude:
        LOG.info("item %d columns already present on base — EXCLUDE-and-replace: %s",
                 item, cols_to_exclude)
        base_select = "base.* EXCLUDE (" + ", ".join(cols_to_exclude) + ")"
    else:
        base_select = "base.*"

    query = f"""
        SELECT
            {base_select},
            {stage_c_cols_sql}
        FROM base
        LEFT JOIN stg
          ON base.crd_number = stg.crd_number
         AND base.version_id = stg.version_id
    """
    LOG.info("running DuckDB LEFT JOIN...")
    joined = con.execute(query).fetch_arrow_table()
    LOG.info("joined rows=%d cols=%d", joined.num_rows, joined.num_columns)
    assert joined.num_rows == base_rows, f"row count mismatch: {joined.num_rows} != {base_rows}"

    # Sanity: rows where item_N_extracted is non-null = stage_c row count
    extracted_count = con.execute(
        f"SELECT count_if(item_{item}_extracted IS NOT NULL) FROM ({query})"
    ).fetchone()[0]
    LOG.info("rows with item_%d_extracted populated: %d (expected stage_c row count)",
             item, extracted_count)

    # Re-emit Lance dataset (overwrite mode keeps prior version for rollback).
    # max_rows_per_file=5000 → ~4 file fragments, each well under R2's 5MB multipart
    # minimum part size so we avoid the "InvalidPart: All non-trailing parts must
    # have the same length" failure mode on large overwrites.
    LOG.info("acquiring lance commit lock...")
    with lance_commit_lock(f"sec_adv_part_2_brochures_lance__add_item{item}"):
        LOG.info("writing new Lance version (overwrite, max_rows_per_file=5000)...")
        lance.write_dataset(
            joined, LANCE_URI, mode="overwrite",
            storage_options=_r2_storage_options(),
            max_rows_per_file=5000,
        )
        new_ds = lance.dataset(LANCE_URI, storage_options=_r2_storage_options())
        LOG.info("new version=%d rows=%d", new_ds.version, new_ds.count_rows())

        # Re-create BTREE indexes (overwrite drops indexes; must rebuild)
        for col in ("crd_number", "version_id", "filing_date"):
            LOG.info("creating BTREE on %s...", col)
            new_ds.create_scalar_index(col, index_type="BTREE", replace=True)

        new_ds.optimize.compact_files()
        new_ds.cleanup_old_versions(older_than=timedelta(days=7))

    # Final verification
    final_ds = lance.dataset(LANCE_URI, storage_options=_r2_storage_options())
    metrics = {
        "base_version": base_version,
        "base_rows": base_rows,
        "new_version": final_ds.version,
        "new_rows": final_ds.count_rows(),
        "new_columns": [c[0] for c in ITEM_COLUMNS[item]],
        "extracted_count": extracted_count,
        "indices": [i["name"] for i in final_ds.list_indices()],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    LOG.info("done: %s", metrics)
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--item", type=int, required=True, choices=sorted(ITEM_COLUMNS))
    ap.add_argument("--stage-c-key", required=True,
                    help="R2 key of stage_c_item{N}_*.parquet (Tier 3 aggregated output)")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    add_item_columns(args.item, args.stage_c_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
