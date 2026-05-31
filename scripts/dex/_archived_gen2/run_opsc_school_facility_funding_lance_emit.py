"""OPSC School Facility Program Funding → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_opsc_school_facility_funding_to_r2.py
from R2, emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/castate/opsc_school_facility_funding_lance

Source columns (30, lowercased in c1 from CSV header) — all VARCHAR in source Parquet:
  county, district, school_name, program, application_number, applicant,
  preliminary_grant_application, full_grant_application, site_and_design_application,
  site_only_application, design_only_application, environmental_hardship_application,
  reduced_to_costs_incurred, number_of_elementary_school_pupil_grants_requested,
  number_of_middle_school_pupil_grants_requested,
  number_of_high_school_pupil_grants_requested,
  number_of_non_severe_school_pupil_grants_requested,
  number_of_severe_school_pupil_grants_requested, grade_level_of_project,
  state_share_of_funding, site_acquisition, financial_hardship, csfa_lease_amount,
  ctefp_loan_amount, type_of_joint_use_facility, type_of_joint_use_partner,
  industry_sector, portables_replaced, last_sab_date, status

BTREE indexes (per audit plan):
  application_number              — canonical PK (one row per SFP application)
  county                          — geographic filter
  applicant_normalized            — entity-name normalized applicant (district / COE)
  last_sab_date_typed             — try_strptime(MM/DD/YYYY) → DATE

Per CLAUDE.md:
  - DuckDB UDF registration uses STRING type names per HEAD 6df6d840 (string args, not the typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (try_strptime + TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_opsc_school_facility_funding_lance_emit.py [--apply]
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

PARQUET_GLOB = f"s3://{R2_BUCKET}/opsc/school-facility-funding/snapshot=*/data.parquet"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/castate/opsc_school_facility_funding_lance"

# castate namespace per state-procurement runbook §"Namespacing conventions"
POLARIS_NAMESPACE = "castate"

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
    """Emit OPSC SFP Lance dataset from latest R2 snapshot."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info("OPSC SFP: %d source rows at %s", row_count, PARQUET_GLOB)
    if row_count == 0:
        raise RuntimeError("OPSC SFP: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            county,
            district,
            school_name,
            program,
            application_number,
            applicant,
            py_normalize_entity(applicant)                                    AS applicant_normalized,
            preliminary_grant_application,
            full_grant_application,
            site_and_design_application,
            site_only_application,
            design_only_application,
            environmental_hardship_application,
            reduced_to_costs_incurred,
            number_of_elementary_school_pupil_grants_requested,
            number_of_middle_school_pupil_grants_requested,
            number_of_high_school_pupil_grants_requested,
            number_of_non_severe_school_pupil_grants_requested,
            number_of_severe_school_pupil_grants_requested,
            grade_level_of_project,
            state_share_of_funding,
            site_acquisition,
            financial_hardship,
            csfa_lease_amount,
            ctefp_loan_amount,
            type_of_joint_use_facility,
            type_of_joint_use_partner,
            industry_sector,
            portables_replaced,
            last_sab_date,
            CAST(try_strptime(last_sab_date, '%m/%d/%Y') AS DATE)             AS last_sab_date_typed,
            status
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("opsc_school_facility_funding_lance"):
        logger.info("writing OPSC SFP Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("OPSC SFP Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in (
            "application_number",
            "county",
            "applicant_normalized",
            "last_sab_date_typed",
        ):
            logger.info("OPSC SFP: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("OPSC SFP: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("OPSC SFP: optimize failed (non-fatal): %s", exc)

    logger.info("OPSC SFP: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "opsc_school_facility_funding_lance",
        "castate.opsc_school_facility_funding_lance — CA DGS Office of Public School Construction "
        "(OPSC) School Facility Program funding awards, weekly snapshot, ~14,200 rows. "
        "BTREE on application_number, county, applicant_normalized, last_sab_date_typed. "
        "Source: data.ca.gov CKAN resource 8080bb19-a63b-47e3-82d3-7451d119e27f.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OPSC School Facility Program Funding → Lance emit"
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
        logger.info("DRY-RUN OPSC SFP: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
