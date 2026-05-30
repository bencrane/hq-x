#!/usr/bin/env python3
"""Bridge generator: SAM-registered entity × SBA PPP borrower — address-keyed.

Closes the address-axis triangle for the PPP universe. The other two legs
already exist:
  - SAM ↔ Overture: `bridges/sam_overture_address_lance` (2.42M, physical only)
  - PPP ↔ Overture: `bridges/ppp_overture_address_lance` (21.85M)

This bridge is the missing direct SAM ↔ PPP leg on the address axis. The
name-axis equivalent `bridges/sam_ppp_borrower_lance` (271K) already exists
for legal-name identity; this bridge is the address corroboration layer
plus the cohort the name path misses (DBA / partial-name / sole-prop PPP
applicants).

Two passes UNION'd into one bridge, tagged by `match_path`:

  match_path = 'physical'
      SAM.physical_address_base_normalized   = PPP.borrower_address_normalized
      SAM.physical_address_state_normalized  = UPPER(PPP.borrstate)
      SAM.physical_address_zip5              = clean5(PPP.borrzip)

  match_path = 'mailing'
      SAM.mailing_address_base_normalized       = PPP.borrower_address_normalized
      UPPER(SAM.mailing_address_state_or_province) = UPPER(PPP.borrstate)
      SAM.mailing_address_zip5                  = clean5(PPP.borrzip)

Inputs are both pre-baked — no Python normalize pass at build time. Symmetric
DDL match on each pass.

Fan-out tiering computed PER match_path:
  platinum = 1:1
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Output: `polaris-warehouse/bridges/sam_ppp_address_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sam_ppp_address')
Floor:  ≥ 30,000 rows union.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_address_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_address_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sam_ppp_address_lance")

BRIDGE_NAME = "sam_ppp_address"
METHOD_NAME = "address_base_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_entities_lance"
SOURCE_RIGHT = "ppp_borrowers_lance"

SAM_ENTITIES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
PPP_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_address_lance"
DATASET_SLUG = "sam_ppp_address_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 30_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load SAM + PPP Arrow tables; both already pre-normalized at emit time."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_ENTITIES_LANCE_URI, storage_options=storage_options)
    sam_cols = [
        "unique_entity_id",
        "legal_business_name",
        "dba_name",
        "entity_url",
        "cage_code",
        "primary_naics",
        "naics_code_string",
        "bus_type_string",
        "sba_business_types_string",
        "govt_bus_poc_first_name",
        "govt_bus_poc_last_name",
        "govt_bus_poc_title",
        "entity_structure",
        "state_of_incorporation",
        "physical_address_line_1",
        "physical_address_line_2",
        "physical_address_city",
        "physical_address_zip5",
        "physical_address_state_normalized",
        "physical_address_base_normalized",
        "mailing_address_line_1",
        "mailing_address_line_2",
        "mailing_address_city",
        "mailing_address_zip5",
        "mailing_address_state_or_province",
        "mailing_address_base_normalized",
        "registration_expiration_date",
        "last_update_date",
    ]
    sam_filter = pc.field("unique_entity_id").is_valid()
    sam_arrow = sam_ds.scanner(columns=sam_cols, filter=sam_filter).to_table()
    rows_left = len(sam_arrow)
    logger.info("  sam_entities_lance (UEI non-null): %d rows", rows_left)

    logger.info("opening sba/ppp_borrowers_lance ...")
    ppp_ds = lance.dataset(PPP_BORROWERS_LANCE_URI, storage_options=storage_options)
    ppp_cols = [
        "legal_name_normalized",
        "borrname_sample",
        "borrstate",
        "borrzip",
        "borrower_address_normalized",
        "total_ppp_loans",
        "total_ppp_approval",
        "max_approval_date",
        "min_approval_date",
        "latest_loanstatus",
        "has_pending_commit",
        "franchise_brands_set",
        "naics_codes_set",
        "lender_set",
    ]
    ppp_filter = (
        pc.field("borrower_address_normalized").is_valid()
        & pc.field("borrstate").is_valid()
        & pc.field("borrzip").is_valid()
        & pc.field("legal_name_normalized").is_valid()
    )
    ppp_arrow = ppp_ds.scanner(columns=ppp_cols, filter=ppp_filter).to_table()
    rows_right = len(ppp_arrow)
    logger.info("  ppp_borrowers_lance (post-filter): %d rows", rows_right)

    return sam_arrow, ppp_arrow, rows_left, rows_right


def _build_match_table(sam_arrow, ppp_arrow, *, bridge_run_id: str, generated_at_iso: str):
    """Two-pass UNION JOIN (physical + mailing) + per-path fan-out tiering."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam", sam_arrow)
    con.register("ppp", ppp_arrow)
    logger.info(
        "  registered: sam=%d  ppp=%d",
        con.execute("SELECT COUNT(*) FROM sam").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM ppp").fetchone()[0],
    )

    # Zip5 normalization mirroring emit_ppp_borrowers_lance.py side-scan
    zip5_expr = (
        "LPAD(SUBSTR(REGEXP_REPLACE(TRIM(CAST({col} AS VARCHAR)), "
        "'(\\.0+|-\\d+).*$', ''), 1, 5), 5, '0')"
    )

    # --- Pass A: physical ---
    logger.info("computing physical pass JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE matched_physical AS
        SELECT
            'physical'                                          AS match_path,
            s.physical_address_base_normalized                  AS address_base_normalized,
            s.physical_address_state_normalized                 AS match_state,
            s.physical_address_zip5                             AS match_zip5,
            s.unique_entity_id                                  AS sam_uei,
            s.legal_business_name                               AS sam_legal_business_name,
            s.dba_name                                          AS sam_dba_name,
            s.entity_url                                        AS sam_entity_url,
            s.cage_code                                         AS sam_cage_code,
            s.primary_naics                                     AS sam_primary_naics,
            s.naics_code_string                                 AS sam_naics_code_string,
            s.bus_type_string                                   AS sam_bus_type_string,
            s.sba_business_types_string                         AS sam_sba_business_types_string,
            s.govt_bus_poc_first_name                           AS sam_govt_poc_first,
            s.govt_bus_poc_last_name                            AS sam_govt_poc_last,
            s.govt_bus_poc_title                                AS sam_govt_poc_title,
            s.entity_structure                                  AS sam_entity_structure,
            s.state_of_incorporation                            AS sam_state_of_incorporation,
            s.physical_address_line_1                           AS sam_address_line_1,
            s.physical_address_line_2                           AS sam_address_line_2,
            s.physical_address_city                             AS sam_address_city,
            s.registration_expiration_date                      AS sam_registration_expiration_date,
            s.last_update_date                                  AS sam_last_update_date,
            p.legal_name_normalized                             AS ppp_legal_name_normalized,
            p.borrname_sample                                   AS ppp_borrname_sample,
            p.borrstate                                         AS ppp_borrstate,
            p.borrzip                                           AS ppp_borrzip,
            p.borrower_address_normalized                       AS ppp_borrower_address_normalized,
            p.total_ppp_loans                                   AS ppp_total_loans,
            p.total_ppp_approval                                AS ppp_total_approval,
            p.max_approval_date                                 AS ppp_max_approval_date,
            p.min_approval_date                                 AS ppp_min_approval_date,
            p.latest_loanstatus                                 AS ppp_latest_loanstatus,
            p.has_pending_commit                                AS ppp_has_pending_commit,
            p.franchise_brands_set                              AS ppp_franchise_brands_set,
            p.naics_codes_set                                   AS ppp_naics_codes_set,
            p.lender_set                                        AS ppp_lender_set
        FROM sam s
        JOIN ppp p
          ON  s.physical_address_base_normalized = p.borrower_address_normalized
          AND s.physical_address_state_normalized = UPPER(TRIM(p.borrstate))
          AND s.physical_address_zip5            = {zip5_expr.format(col='p.borrzip')}
        WHERE s.physical_address_base_normalized IS NOT NULL
          AND s.physical_address_state_normalized IS NOT NULL
          AND s.physical_address_zip5 IS NOT NULL
        """
    )
    rows_p = con.execute("SELECT COUNT(*) FROM matched_physical").fetchone()[0]
    logger.info("  physical pass matched (pre-tier): %d rows", rows_p)

    # --- Pass B: mailing ---
    logger.info("computing mailing pass JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE matched_mailing AS
        SELECT
            'mailing'                                           AS match_path,
            s.mailing_address_base_normalized                   AS address_base_normalized,
            UPPER(TRIM(s.mailing_address_state_or_province))    AS match_state,
            s.mailing_address_zip5                              AS match_zip5,
            s.unique_entity_id                                  AS sam_uei,
            s.legal_business_name                               AS sam_legal_business_name,
            s.dba_name                                          AS sam_dba_name,
            s.entity_url                                        AS sam_entity_url,
            s.cage_code                                         AS sam_cage_code,
            s.primary_naics                                     AS sam_primary_naics,
            s.naics_code_string                                 AS sam_naics_code_string,
            s.bus_type_string                                   AS sam_bus_type_string,
            s.sba_business_types_string                         AS sam_sba_business_types_string,
            s.govt_bus_poc_first_name                           AS sam_govt_poc_first,
            s.govt_bus_poc_last_name                            AS sam_govt_poc_last,
            s.govt_bus_poc_title                                AS sam_govt_poc_title,
            s.entity_structure                                  AS sam_entity_structure,
            s.state_of_incorporation                            AS sam_state_of_incorporation,
            s.mailing_address_line_1                            AS sam_address_line_1,
            s.mailing_address_line_2                            AS sam_address_line_2,
            s.mailing_address_city                              AS sam_address_city,
            s.registration_expiration_date                      AS sam_registration_expiration_date,
            s.last_update_date                                  AS sam_last_update_date,
            p.legal_name_normalized                             AS ppp_legal_name_normalized,
            p.borrname_sample                                   AS ppp_borrname_sample,
            p.borrstate                                         AS ppp_borrstate,
            p.borrzip                                           AS ppp_borrzip,
            p.borrower_address_normalized                       AS ppp_borrower_address_normalized,
            p.total_ppp_loans                                   AS ppp_total_loans,
            p.total_ppp_approval                                AS ppp_total_approval,
            p.max_approval_date                                 AS ppp_max_approval_date,
            p.min_approval_date                                 AS ppp_min_approval_date,
            p.latest_loanstatus                                 AS ppp_latest_loanstatus,
            p.has_pending_commit                                AS ppp_has_pending_commit,
            p.franchise_brands_set                              AS ppp_franchise_brands_set,
            p.naics_codes_set                                   AS ppp_naics_codes_set,
            p.lender_set                                        AS ppp_lender_set
        FROM sam s
        JOIN ppp p
          ON  s.mailing_address_base_normalized = p.borrower_address_normalized
          AND UPPER(TRIM(s.mailing_address_state_or_province)) = UPPER(TRIM(p.borrstate))
          AND s.mailing_address_zip5            = {zip5_expr.format(col='p.borrzip')}
        WHERE s.mailing_address_base_normalized IS NOT NULL
          AND s.mailing_address_state_or_province IS NOT NULL
          AND s.mailing_address_zip5 IS NOT NULL
        """
    )
    rows_m = con.execute("SELECT COUNT(*) FROM matched_mailing").fetchone()[0]
    logger.info("  mailing pass matched (pre-tier): %d rows", rows_m)

    # UNION + per-path fan-out
    logger.info("computing per-path fan-out + tiered output ...")
    con.execute(
        """
        CREATE TEMP TABLE matched_all AS
        SELECT * FROM matched_physical
        UNION ALL
        SELECT * FROM matched_mailing
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_uei, match_path, COUNT(*) AS sam_fan_out
        FROM matched_all
        GROUP BY sam_uei, match_path
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ppp_fanout AS
        SELECT ppp_legal_name_normalized AS k1,
               ppp_borrstate              AS k2,
               ppp_borrzip                AS k3,
               match_path,
               COUNT(*)                   AS ppp_fan_out
        FROM matched_all
        GROUP BY 1, 2, 3, 4
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            sf.sam_fan_out,
            pf.ppp_fan_out,
            CASE
                WHEN sf.sam_fan_out > {COLLISION_THRESHOLD}
                  OR pf.ppp_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_fan_out = 1 AND pf.ppp_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sam_fan_out = 1 OR  pf.ppp_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched_all m
        JOIN sam_fanout sf
          ON sf.sam_uei = m.sam_uei AND sf.match_path = m.match_path
        JOIN ppp_fanout pf
          ON pf.k1 = m.ppp_legal_name_normalized
         AND pf.k2 = m.ppp_borrstate
         AND pf.k3 = m.ppp_borrzip
         AND pf.match_path = m.match_path
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS "
        "SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'"
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE confidence_tier='platinum'),
          COUNT(*) FILTER (WHERE confidence_tier='gold'),
          COUNT(*) FILTER (WHERE confidence_tier='silver'),
          COUNT(*) FILTER (WHERE match_path='physical'),
          COUNT(*) FILTER (WHERE match_path='mailing')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_path_physical": row_counts[4],
        "rows_path_mailing": row_counts[5],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("sam_uei", "ppp_legal_name_normalized", "address_base_normalized"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE index created on %s", col)
            except Exception as e:
                logger.warning("BTREE index on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (address_base_normalized, 2-letter US state, "
            "5-digit zip). Applies _lib/address_normalize.py "
            f"v{ADDR_NORMALIZER_VERSION} (base form: unit-stripped). Both sides "
            "pre-normalized at emit time — no UDF calls during this JOIN."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/address_normalize.py",
        normalizer_version=ADDR_NORMALIZER_VERSION,
        blacklist_module="_lib/address_normalize.py",
        blacklist_version=ADDR_NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=[
            "physical_address_base_normalized",
            "mailing_address_base_normalized",
            "physical_address_state_normalized",
            "mailing_address_state_or_province",
            "physical_address_zip5",
            "mailing_address_zip5",
        ],
        input_columns_right=[
            "borrower_address_normalized",
            "borrstate",
            "borrzip",
        ],
        output_value_description=(
            "normalized USPS-abbrev street + 2-letter state + 5-digit zip join key; "
            "two passes (physical, mailing) UNION'd with match_path tag"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM entities × SBA PPP borrowers — address-keyed exact match. "
            "Two passes UNION'd: SAM physical × PPP and SAM mailing × PPP, "
            "tagged by match_path. Closes the SAM × PPP address-axis leg. "
            "Sister to the name-axis sam_ppp_borrower_lance bridge; together "
            "they form the complete SAM × PPP identity layer."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info(
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, ADDR_NORMALIZER_VERSION,
    )
    logger.info("inputs: sam_gov/entities_lance + sba/ppp_borrowers_lance (Arrow-bridge, pre-baked)")
    logger.info("output: %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        sam_arrow, ppp_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_arrow, ppp_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info("    silver   (N:M ≤%d):     %d", COLLISION_THRESHOLD, counts["rows_tier3"])
        logger.info("  by match_path:")
        logger.info("    physical: %d", counts["rows_path_physical"])
        logger.info("    mailing:  %d", counts["rows_path_mailing"])
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info(
                "DRY RUN OK — no Lance / Postgres writes.  duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_path_physical": counts["rows_path_physical"],
                "rows_path_mailing": counts["rows_path_mailing"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
