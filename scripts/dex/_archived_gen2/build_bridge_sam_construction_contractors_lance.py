"""SAM.gov construction contractors cohort → Lance emit (Pattern A enriched-cohort).

UEI-grain rollup of SAM.gov registered entities with construction primary NAICS
(23xxxx), enriched with USAspending prime-contract history flags. Audience-
targeting payload for the EquipmentWork heavy-iron newsletter — the
"registered in SAM for construction work, with or without ever winning a
federal prime" cohort. Downstream slicing:

- "Never won a federal prime" cohort = WHERE has_ever_won_prime = FALSE
- "Heavy-iron registrants only"       = WHERE is_heavy_iron_naics = TRUE
- "Heavy-iron sub-proxy" (registered  = WHERE is_heavy_iron_naics = TRUE
   for heavy iron, never won prime)     AND has_ever_won_prime = FALSE

Inputs:
- s3://.../polaris-warehouse/sam_gov/entities_lance         (884,203 rows)
- s3://.../polaris-warehouse/usaspending/contracts_lance    (15.5M rows)

Output:
- s3://.../polaris-warehouse/bridges/sam_construction_contractors_lance
  UEI-grain, BTREE on: uei, physical_address_state, is_heavy_iron_naics,
  has_ever_won_prime.

Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A enriched-cohort emit":
NOT a new identity bridge — no rows in ops.bridges / ops.match_method_versions.
Per L28: cohort emit carries its own fresh bridge_run_id UUID for provenance.

Per L17: every row carries bridge_run_id UUID.
Per L29: TRY_CAST for VARCHAR -> typed casts on USAspending dollar / date fields.

Lance read path follows DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern B Template":
  lance.dataset(uri).scanner(columns=..., filter=...).to_table()
   → con.register('name', arrow_table) → DuckDB SQL

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/build_bridge_sam_construction_contractors_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants ──────────────────────────────────────────────────

R2_BUCKET = "dex-raw-landing-zone"

SAM_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sam_gov/entities_lance"
USA_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contracts_lance"

OUTPUT_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/sam_construction_contractors_lance"
)
DATASET_SLUG = "sam_construction_contractors_lance"
POLARIS_NAMESPACE = "bridges"

# Floor: SAM has ~884K entities total. Construction sector typically ~5-15% of
# small-business federal registrations. Floor at 30K leaves slack on a ~70-90K
# baseline expectation.
MIN_ROW_FLOOR = 30_000

EMIT_VERSION = "1.0.0"

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
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    script = Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
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
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def _read_sam_constr_arrow(storage_options: dict):
    """Read SAM entities filtered to construction primary NAICS via Lance scanner.

    Lance filter is a SQL string (DataFusion backend); `starts_with` via
    `pyarrow.compute` doesn't translate to Substrait, so use `LIKE` here.
    """
    import lance

    logger.info("scanning SAM entities for construction primary_naics ...")
    ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam = ds.scanner(
        columns=[
            "uei_normalized",
            "legal_business_name",
            "dba_name",
            "primary_naics",
            "naics_code_string",
            "entity_structure",
            "state_of_incorporation",
            "physical_address_line_1",
            "physical_address_city",
            "physical_address_province_or_state",
            "physical_address_zippostal_code",
            "govt_bus_poc_first_name",
            "govt_bus_poc_last_name",
            "govt_bus_poc_title",
            "initial_registration_date",
            "registration_expiration_date",
            "exclusion_status_flag",
        ],
        filter="primary_naics LIKE '23%' AND uei_normalized IS NOT NULL",
    ).to_table()
    logger.info("SAM construction rows read: %d", sam.num_rows)
    return sam


def _read_us_primes_arrow(storage_options: dict):
    """Read USAspending contracts (UEI, NAICS, action_date, $) — projection only."""
    import lance

    logger.info("scanning USAspending contracts (recipient_uei + NAICS + dollars) ...")
    ds = lance.dataset(USA_LANCE_URI, storage_options=storage_options)
    us = ds.scanner(
        columns=[
            "recipient_uei",
            "naics_code",
            "action_date",
            "federal_action_obligation",
        ],
        filter="recipient_uei IS NOT NULL",
    ).to_table()
    logger.info("USAspending prime contract rows read: %d", us.num_rows)
    return us


def emit() -> int:
    """Build SAM-construction × USAspending-primes enriched cohort and write Lance."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    emit_run_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    logger.info("emit_run_id=%s generated_at=%s", emit_run_id, generated_at)

    storage_options = _storage_options()

    sam_arrow = _read_sam_constr_arrow(storage_options)
    if sam_arrow.num_rows < MIN_ROW_FLOOR:
        raise RuntimeError(
            f"SAM construction registrants ({sam_arrow.num_rows}) below floor {MIN_ROW_FLOOR}"
        )

    us_arrow = _read_us_primes_arrow(storage_options)

    con = _duckdb_conn()
    con.register("sam_raw", sam_arrow)
    con.register("us_raw", us_arrow)

    # --- 1. Construction-filtered SAM view with derived flags ---------------
    con.execute(
        """
        CREATE TEMP TABLE sam_constr AS
        SELECT
            uei_normalized                                              AS uei,
            legal_business_name,
            dba_name,
            primary_naics,
            naics_code_string,
            SUBSTR(primary_naics, 1, 3)                                 AS naics_3,
            CASE SUBSTR(primary_naics, 1, 3)
                WHEN '236' THEN 'Construction of Buildings'
                WHEN '237' THEN 'Heavy & Civil Engineering'
                WHEN '238' THEN 'Specialty Trade Contractors'
            END                                                         AS naics_sector,
            CAST(primary_naics LIKE '237%' OR primary_naics = '238910' AS BOOLEAN)
                                                                        AS is_heavy_iron_naics,
            entity_structure,
            state_of_incorporation,
            physical_address_line_1,
            physical_address_city,
            physical_address_province_or_state                          AS physical_address_state,
            physical_address_zippostal_code                             AS physical_address_zip,
            govt_bus_poc_first_name,
            govt_bus_poc_last_name,
            govt_bus_poc_title,
            initial_registration_date,
            registration_expiration_date,
            exclusion_status_flag
        FROM sam_raw
        """
    )
    sam_n = con.execute("SELECT count(*) FROM sam_constr").fetchone()[0]
    logger.info("SAM construction (in DuckDB): %d rows", sam_n)

    # --- 2. Aggregate USAspending primes per recipient_uei (FILTER aggregates) -
    # Per L29: TRY_CAST on dollar + date VARCHAR cols.
    logger.info("aggregating USAspending primes per recipient_uei ...")
    con.execute(
        """
        CREATE TEMP TABLE us_agg AS
        SELECT
            recipient_uei AS uei,
            COUNT(*)                                                                                  AS prime_actions_count,
            SUM(TRY_CAST(federal_action_obligation AS DOUBLE))                                        AS total_prime_obligations,
            MAX(TRY_CAST(action_date AS DATE))                                                        AS last_prime_action_date,
            COUNT(*) FILTER (WHERE naics_code LIKE '23%')                                             AS construction_actions_count,
            SUM(TRY_CAST(federal_action_obligation AS DOUBLE)) FILTER (WHERE naics_code LIKE '23%')   AS construction_prime_obligations,
            MAX(TRY_CAST(action_date AS DATE)) FILTER (WHERE naics_code LIKE '23%')                   AS last_construction_prime_action_date,
            COUNT(*) FILTER (WHERE naics_code LIKE '237%' OR naics_code = '238910')                   AS heavy_iron_actions_count,
            SUM(TRY_CAST(federal_action_obligation AS DOUBLE)) FILTER (WHERE naics_code LIKE '237%' OR naics_code = '238910')
                                                                                                      AS heavy_iron_prime_obligations,
            MAX(TRY_CAST(action_date AS DATE)) FILTER (WHERE naics_code LIKE '237%' OR naics_code = '238910')
                                                                                                      AS last_heavy_iron_prime_action_date
        FROM us_raw
        GROUP BY recipient_uei
        """
    )
    us_n = con.execute("SELECT count(*) FROM us_agg").fetchone()[0]
    logger.info("USAspending distinct prime recipient UEIs: %d", us_n)

    # --- 3. LEFT JOIN: SAM spine + USAspending enrichment ---------------------
    logger.info("joining SAM ← USAspending and projecting cohort rows ...")
    reader = con.execute(
        f"""
        SELECT
            s.uei,
            s.legal_business_name,
            s.dba_name,
            s.primary_naics,
            s.naics_code_string,
            s.naics_3,
            s.naics_sector,
            s.is_heavy_iron_naics,
            s.entity_structure,
            s.state_of_incorporation,
            s.physical_address_line_1,
            s.physical_address_city,
            s.physical_address_state,
            s.physical_address_zip,
            s.govt_bus_poc_first_name,
            s.govt_bus_poc_last_name,
            s.govt_bus_poc_title,
            s.initial_registration_date,
            s.registration_expiration_date,
            s.exclusion_status_flag,

            -- USAspending enrichment flags + counters
            CAST(u.uei IS NOT NULL AS BOOLEAN)                                  AS has_ever_won_prime,
            CAST(COALESCE(u.construction_actions_count,0) > 0 AS BOOLEAN)       AS has_ever_won_construction_prime,
            CAST(COALESCE(u.heavy_iron_actions_count,0) > 0 AS BOOLEAN)         AS has_ever_won_heavy_iron_prime,
            COALESCE(u.prime_actions_count, 0)                                  AS prime_actions_count,
            COALESCE(u.construction_actions_count, 0)                           AS construction_actions_count,
            COALESCE(u.heavy_iron_actions_count, 0)                             AS heavy_iron_actions_count,
            COALESCE(u.total_prime_obligations, 0.0)                            AS total_prime_obligations,
            COALESCE(u.construction_prime_obligations, 0.0)                     AS construction_prime_obligations,
            COALESCE(u.heavy_iron_prime_obligations, 0.0)                       AS heavy_iron_prime_obligations,
            u.last_prime_action_date,
            u.last_construction_prime_action_date,
            u.last_heavy_iron_prime_action_date,

            -- Provenance (L17): every row carries the cohort's emit-run UUID
            '{emit_run_id}'                                                     AS bridge_run_id,
            '{EMIT_VERSION}'                                                    AS emit_version,
            TIMESTAMP '{generated_at}'                                          AS generated_at
        FROM sam_constr s
        LEFT JOIN us_agg u ON u.uei = s.uei
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=20_000,
        )
        rows = ds.count_rows()
        logger.info("Lance written: %d rows (version %s)", rows, ds.version)
        if rows < MIN_ROW_FLOOR:
            raise RuntimeError(f"HARD FAIL: rows ({rows}) < floor ({MIN_ROW_FLOOR})")

        for col in ("uei", "physical_address_state", "is_heavy_iron_naics", "has_ever_won_prime"):
            logger.info("BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("optimize failed (non-fatal): %s", exc)

    logger.info("cohort emit complete — rows=%d uri=%s", rows, OUTPUT_LANCE_URI)

    _register_polaris(
        DATASET_SLUG,
        "bridges.sam_construction_contractors_lance — SAM.gov entities registered with "
        "construction primary NAICS (23xxxx), enriched with USAspending prime-contract "
        "history flags. EquipmentWork heavy-iron newsletter audience cohort. UEI-grain. "
        "BTREE on uei, physical_address_state, is_heavy_iron_naics, has_ever_won_prime. "
        "Pattern A enriched-cohort emit per DATA-FACTORY-ARCHITECTURE-PATTERNS.md.",
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SAM construction contractors → Lance enriched-cohort emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        sam = _read_sam_constr_arrow(_storage_options())
        logger.info(
            "DRY-RUN: %d SAM construction registrants would be cohort spine "
            "(pass --apply to emit)",
            sam.num_rows,
        )
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
