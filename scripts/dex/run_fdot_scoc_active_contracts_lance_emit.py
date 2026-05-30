"""FDOT SCOC active + historical contracts → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_fdot_scoc_active_contracts_to_r2.py
from R2, emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/fdot/scoc_active_contracts_lance

Source columns (30, snake_case in source Parquet — see c1 _COLUMN_RENAME):
  active, district, contract_id, status, work_begin, project_id, description,
  work_mix, county, vendor_name, vendor_id, project_engineer_manager_name,
  original_contract_days, adjusted_contract_days, current_contract_days,
  project_type, days_used, original_contract_amount, current_contract_amount,
  estimate_paid_to_date, project_work_type, contract_type, letting_date,
  award_date, executed_date, time_began_date, final_acceptance_date,
  start_date, estimated_completion_date, has_map_url

Derived typed siblings (per L29):
  district_number                 — extracted from "District 01" via regexp
  original_contract_amount_typed  — TRY_CAST(... AS DOUBLE)
  current_contract_amount_typed   — TRY_CAST(... AS DOUBLE)
  estimate_paid_to_date_typed     — TRY_CAST(... AS DOUBLE)
  change_order_amount_typed       — current_typed - original_typed
  *_days_typed                    — TRY_CAST(... AS INTEGER) for 4 days columns
  letting_date_typed              — TRY_CAST(... AS DATE)
  award_date_typed                — TRY_CAST(... AS DATE)
  executed_date_typed             — TRY_CAST(... AS DATE)
  time_began_date_typed           — TRY_CAST(... AS DATE)
  final_acceptance_date_typed     — TRY_CAST(... AS DATE)
  start_date_typed                — TRY_CAST(... AS DATE)
  estimated_completion_date_typed — TRY_CAST(... AS DATE)
  work_begin_typed                — TRY_CAST(... AS DATE)
  vendor_name_normalized          — py_normalize_entity UDF
  project_engineer_manager_normalized — lowercase + trim (proj engs are
                                        person names — entity normalizer
                                        would strip them; keep simple)

BTREE indexes:
  contract_id                          — canonical PK (one row per FDOT contract)
  vendor_id                            — winning contractor stable ID
  vendor_name_normalized               — name-based bridge to FL contractor identity
  project_engineer_manager_normalized  — FDOT-side contact bridge
  letting_date_typed                   — time-range filter

Per CLAUDE.md:
  - DuckDB UDF registration uses STRING type names per HEAD 6df6d840 (string args, not the typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_fdot_scoc_active_contracts_lance_emit.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock
from scripts._lib.entity_name_normalize import normalize_entity_name

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB = f"s3://{R2_BUCKET}/fdot/scoc-active-contracts/snapshot=*/data.parquet"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/fdot/scoc_active_contracts_lance"

# fdot namespace (analogous to caltrans for CA DOT) per state-procurement runbook
POLARIS_NAMESPACE = "fdot"

TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE s3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{os.environ["R2_ENDPOINT"].replace("https://", "")}',
            REGION 'us-east-1',
            URL_STYLE 'path'
        )
        """
    )
    # DuckDB UDF registration: STRING type names per HEAD 6df6d840 (string args, not the typing module)
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register Lance dataset as a Polaris Generic Table."""
    script = (
        Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    )
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=60)
        logger.info("Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name)
    except subprocess.CalledProcessError as exc:
        logger.warning("Polaris registration failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def emit() -> None:
    """Emit FDOT SCOC Lance dataset from latest R2 snapshot(s)."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info("FDOT SCOC: %d source rows at %s", row_count, PARQUET_GLOB)
    if row_count == 0:
        raise RuntimeError("FDOT SCOC: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    # Project, derive typed siblings, normalize identity fields. Note the
    # district-number extraction: upstream value is "District 01" (2-digit zero-
    # padded). regexp_extract pulls the trailing digits as VARCHAR; downstream
    # callers TRY_CAST to INT as needed.
    reader = con.execute(
        f"""
        SELECT
            active,
            district,
            regexp_extract(district, '([0-9]+)', 1)                            AS district_number,
            contract_id,
            status,
            work_begin,
            TRY_CAST(work_begin AS DATE)                                       AS work_begin_typed,
            project_id,
            description,
            work_mix,
            county,
            vendor_name,
            vendor_id,
            py_normalize_entity(vendor_name)                                   AS vendor_name_normalized,
            project_engineer_manager_name,
            lower(trim(project_engineer_manager_name))                         AS project_engineer_manager_normalized,
            original_contract_days,
            TRY_CAST(original_contract_days AS INTEGER)                        AS original_contract_days_typed,
            adjusted_contract_days,
            TRY_CAST(adjusted_contract_days AS INTEGER)                        AS adjusted_contract_days_typed,
            current_contract_days,
            TRY_CAST(current_contract_days AS INTEGER)                         AS current_contract_days_typed,
            project_type,
            days_used,
            TRY_CAST(days_used AS INTEGER)                                     AS days_used_typed,
            original_contract_amount,
            TRY_CAST(original_contract_amount AS DOUBLE)                       AS original_contract_amount_typed,
            current_contract_amount,
            TRY_CAST(current_contract_amount AS DOUBLE)                        AS current_contract_amount_typed,
            estimate_paid_to_date,
            TRY_CAST(estimate_paid_to_date AS DOUBLE)                          AS estimate_paid_to_date_typed,
            (TRY_CAST(current_contract_amount AS DOUBLE)
              - TRY_CAST(original_contract_amount AS DOUBLE))                  AS change_order_amount_typed,
            project_work_type,
            contract_type,
            letting_date,
            TRY_CAST(letting_date AS DATE)                                     AS letting_date_typed,
            award_date,
            TRY_CAST(award_date AS DATE)                                       AS award_date_typed,
            executed_date,
            TRY_CAST(executed_date AS DATE)                                    AS executed_date_typed,
            time_began_date,
            TRY_CAST(time_began_date AS DATE)                                  AS time_began_date_typed,
            final_acceptance_date,
            TRY_CAST(final_acceptance_date AS DATE)                            AS final_acceptance_date_typed,
            start_date,
            TRY_CAST(start_date AS DATE)                                       AS start_date_typed,
            estimated_completion_date,
            TRY_CAST(estimated_completion_date AS DATE)                        AS estimated_completion_date_typed,
            has_map_url
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("scoc_active_contracts_lance"):
        logger.info("writing FDOT SCOC Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("FDOT SCOC Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in (
            "contract_id",
            "vendor_id",
            "vendor_name_normalized",
            "project_engineer_manager_normalized",
            "letting_date_typed",
        ):
            logger.info("FDOT SCOC: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("FDOT SCOC: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("FDOT SCOC: optimize failed (non-fatal): %s", exc)

    logger.info("FDOT SCOC: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "scoc_active_contracts_lance",
        "fdot.scoc_active_contracts_lance — Florida DOT State Construction Office active + "
        "historical contract roster, ~1,590 rows. Every row carries winning contractor "
        "(vendor_name + vendor_id) AND FDOT-side Project Engineer/Manager. BTREE on "
        "contract_id, vendor_id, vendor_name_normalized, project_engineer_manager_normalized, "
        "letting_date_typed. Source: scoc.fdot.gov Active Contracts Export.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FDOT SCOC active + historical contracts → Lance emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        con = _duckdb_conn()
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
        ).fetchone()[0]
        logger.info("DRY-RUN FDOT SCOC: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
