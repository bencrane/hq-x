#!/usr/bin/env python3
"""Bridge generator: SAM × SBA PPP — high-confidence intersection of name AND address.

Materializes the intersection of two existing bridges:

  - `bridges/sam_ppp_borrower_lance`  (legal_name + state)
  - `bridges/sam_ppp_address_lance`   (address_base_normalized + state + zip5,
                                       physical OR mailing pass)

INNER JOIN on `(sam_uei, ppp_legal_name_normalized, ppp_borrstate)`. Every
row in the output represents a (SAM UEI ↔ PPP borrower) pair where BOTH
the legal-name match AND the address match agreed. This is the
highest-precision SAM × PPP identity bridge — name agrees on entity
identity, address corroborates on physical or mailing location.

Composite confidence tier = `min(name_tier, address_tier)` ordered as
platinum > gold > silver. If a (UEI, PPP) pair has multiple address-side
rows (e.g. matched on BOTH physical AND mailing, or matched on multiple
fan-out rows), the per-pair address row is collapsed by taking the WORST
tier across them and aggregating match_paths.

Why a separate bridge:
  Consumers asking "is this SAM UEI the same legal entity as this PPP
  borrower" benefit from a pre-materialized high-confidence cohort. The
  alternative is to ask consumers to compose the two bridges themselves
  every time — duplicative and easy to get wrong. This bridge bakes the
  intersection as a load-bearing artifact.

Floor: ≥ 10,000 rows. Intersection is smaller than either input by
construction (entities must agree on BOTH name and address).

Output: `polaris-warehouse/bridges/sam_ppp_borrower_with_address_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sam_ppp_borrower_with_address')

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_borrower_with_address_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_borrower_with_address_lance.py --dry-run
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
from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NAME_NORMALIZER_VERSION,
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
logger = logging.getLogger("build_bridge_sam_ppp_borrower_with_address_lance")

BRIDGE_NAME = "sam_ppp_borrower_with_address"
METHOD_NAME = "name_state_with_address_corroboration_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_ppp_borrower_lance"
SOURCE_RIGHT = "sam_ppp_address_lance"

NAME_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_borrower_lance"
ADDRESS_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_address_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_borrower_with_address_lance"
DATASET_SLUG = "sam_ppp_borrower_with_address_lance"

MIN_ROWS_MATCHED = 10_000
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
    """Load both source bridges as Arrow tables."""
    import lance

    logger.info("opening name bridge: %s ...", NAME_BRIDGE_URI)
    name_ds = lance.dataset(NAME_BRIDGE_URI, storage_options=storage_options)
    name_arrow = name_ds.to_table()
    rows_name = len(name_arrow)
    logger.info("  sam_ppp_borrower_lance: %d rows", rows_name)

    logger.info("opening address bridge: %s ...", ADDRESS_BRIDGE_URI)
    addr_ds = lance.dataset(ADDRESS_BRIDGE_URI, storage_options=storage_options)
    addr_arrow = addr_ds.to_table()
    rows_addr = len(addr_arrow)
    logger.info("  sam_ppp_address_lance: %d rows", rows_addr)

    return name_arrow, addr_arrow, rows_name, rows_addr


def _build_match_table(name_arrow, addr_arrow, *, bridge_run_id: str, generated_at_iso: str):
    """Inner-join the two bridges on (sam_uei, ppp_legal_name_normalized, ppp_borrstate).

    Address bridge may contribute multiple rows per (sam_uei, ppp identity)
    pair (e.g. physical AND mailing both matched, or multi-tenant fan-out).
    Collapse the address side per-pair to one row carrying:
      - aggregated match_paths (sorted, pipe-joined: 'mailing', 'physical',
        or 'mailing|physical' when both matched)
      - worst (highest-numbered) address-side tier
      - max fan-outs from the address side
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("name_b", name_arrow)
    con.register("addr_b", addr_arrow)

    # Collapse address-side per (sam_uei, ppp identity).
    logger.info("collapsing address-side per pair ...")
    con.execute(
        """
        CREATE TEMP TABLE addr_collapsed AS
        SELECT
            sam_uei,
            ppp_legal_name_normalized,
            ppp_borrstate,
            ppp_borrzip,
            STRING_AGG(DISTINCT match_path, '|' ORDER BY match_path) AS address_match_paths,
            -- worst tier (highest-numbered) across the per-pair rows
            CASE
              WHEN MAX(CASE confidence_tier
                         WHEN 'silver' THEN 3
                         WHEN 'gold'   THEN 2
                         WHEN 'platinum' THEN 1
                         ELSE 4
                       END) = 1 THEN 'platinum'
              WHEN MAX(CASE confidence_tier
                         WHEN 'silver' THEN 3
                         WHEN 'gold'   THEN 2
                         WHEN 'platinum' THEN 1
                         ELSE 4
                       END) = 2 THEN 'gold'
              ELSE 'silver'
            END AS address_confidence_tier,
            MAX(sam_fan_out)  AS address_sam_fan_out,
            MAX(ppp_fan_out)  AS address_ppp_fan_out,
            MAX(address_base_normalized) AS address_base_normalized,
            MAX(match_zip5)              AS address_match_zip5,
            -- audit: collect SAM address line(s) seen across paths
            MAX(sam_address_line_1)      AS sam_address_line_1_sample,
            MAX(sam_address_city)        AS sam_address_city_sample
        FROM addr_b
        GROUP BY sam_uei, ppp_legal_name_normalized, ppp_borrstate, ppp_borrzip
        """
    )
    rows_collapsed = con.execute("SELECT COUNT(*) FROM addr_collapsed").fetchone()[0]
    logger.info("  address-side pairs (collapsed): %d", rows_collapsed)

    # Inner join name bridge × collapsed address bridge.
    # Note: name bridge keys are (sam_uei, ppp_name_normalized [= legal_name_normalized], ppp_borrstate);
    # the address bridge uses ppp_legal_name_normalized — same value, different alias.
    logger.info("inner-joining name × address ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            n.sam_uei,
            n.ppp_name_normalized                                AS ppp_legal_name_normalized,
            n.ppp_borrstate,
            n.ppp_borrname_sample,
            n.ppp_borrzip,
            n.ppp_borrower_address_normalized,
            -- name-axis provenance
            n.match_value_normalized                             AS name_match_value_normalized,
            n.match_state                                        AS name_match_state,
            n.confidence_tier                                    AS name_confidence_tier,
            n.sam_fan_out                                        AS name_sam_fan_out,
            n.ppp_fan_out                                        AS name_ppp_fan_out,
            -- address-axis provenance
            a.address_match_paths,
            a.address_confidence_tier,
            a.address_sam_fan_out,
            a.address_ppp_fan_out,
            a.address_base_normalized,
            a.address_match_zip5,
            a.sam_address_line_1_sample,
            a.sam_address_city_sample,
            -- composite tier: worst of name and address
            CASE
              WHEN GREATEST(
                     CASE n.confidence_tier WHEN 'silver' THEN 3 WHEN 'gold' THEN 2 WHEN 'platinum' THEN 1 ELSE 4 END,
                     CASE a.address_confidence_tier WHEN 'silver' THEN 3 WHEN 'gold' THEN 2 WHEN 'platinum' THEN 1 ELSE 4 END
                   ) = 1 THEN 'platinum'
              WHEN GREATEST(
                     CASE n.confidence_tier WHEN 'silver' THEN 3 WHEN 'gold' THEN 2 WHEN 'platinum' THEN 1 ELSE 4 END,
                     CASE a.address_confidence_tier WHEN 'silver' THEN 3 WHEN 'gold' THEN 2 WHEN 'platinum' THEN 1 ELSE 4 END
                   ) = 2 THEN 'gold'
              ELSE 'silver'
            END                                                  AS composite_confidence_tier,
            -- SAM authoritative payload (from name bridge — single canonical source)
            n.sam_legal_business_name,
            n.sam_dba_name,
            n.sam_entity_url,
            n.sam_cage_code,
            n.sam_primary_naics,
            n.sam_naics_code_string,
            n.sam_bus_type_string,
            n.sam_sba_business_types_string,
            n.sam_govt_poc_first,
            n.sam_govt_poc_last,
            n.sam_govt_poc_title,
            n.sam_entity_structure,
            n.sam_state_of_incorporation,
            n.sam_purpose_of_registration,
            n.sam_city,
            n.sam_zip5,
            n.sam_registration_expiration_date,
            n.sam_last_update_date,
            -- PPP capital-deployment payload
            n.ppp_total_loans,
            n.ppp_total_approval,
            n.ppp_max_approval_date,
            n.ppp_min_approval_date,
            n.ppp_latest_loanstatus,
            n.ppp_has_pending_commit,
            n.ppp_franchise_brands_set,
            n.ppp_naics_codes_set,
            n.ppp_lender_set,
            TIMESTAMP '{generated_at_iso}'                       AS generated_at,
            '{BRIDGE_VERSION}'                                   AS bridge_version,
            '{bridge_run_id}'                                    AS bridge_run_id
        FROM name_b n
        JOIN addr_collapsed a
          ON  a.sam_uei                    = n.sam_uei
          AND a.ppp_legal_name_normalized  = n.ppp_name_normalized
          AND a.ppp_borrstate              = n.ppp_borrstate
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS SELECT * FROM bridge_all"
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE composite_confidence_tier='platinum'),
          COUNT(*) FILTER (WHERE composite_confidence_tier='gold'),
          COUNT(*) FILTER (WHERE composite_confidence_tier='silver'),
          COUNT(*) FILTER (WHERE address_match_paths='physical'),
          COUNT(*) FILTER (WHERE address_match_paths='mailing'),
          COUNT(*) FILTER (WHERE address_match_paths='mailing|physical')
        FROM bridge_match
        """
    ).fetchone()

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_path_physical_only": row_counts[4],
        "rows_path_mailing_only": row_counts[5],
        "rows_path_both": row_counts[6],
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
        for col in ("sam_uei", "ppp_legal_name_normalized", "composite_confidence_tier"):
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
            "Composite SAM × PPP match requiring BOTH (a) name+state exact match "
            "via name_state_exact AND (b) address+state+zip5 exact match via "
            "address_base_state_zip_exact on either physical or mailing SAM-side "
            "address. Inner join of the two sister bridges."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py + _lib/address_normalize.py",
        normalizer_version=f"name v{NAME_NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        blacklist_module="(same as component normalizers)",
        blacklist_version=f"name v{NAME_NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        tier_rule_description=(
            "composite_tier = worst of name_tier and address_tier "
            "(platinum > gold > silver). No 'rejected' rows by construction — "
            "both source bridges already excluded rejected rows."
        ),
        rejection_rule_description=(
            "no rejection at this layer; relies on source bridges' >50 fan-out cap"
        ),
        input_columns_left=[
            "sam_uei", "ppp_name_normalized", "ppp_borrstate", "confidence_tier",
        ],
        input_columns_right=[
            "sam_uei", "ppp_legal_name_normalized", "ppp_borrstate",
            "match_path", "confidence_tier",
        ],
        output_value_description=(
            "(sam_uei, ppp_legal_name_normalized, ppp_borrstate) triple where "
            "BOTH name-axis and address-axis bridges produced a match. "
            "Carries name_match_*, address_match_paths/tier, composite_confidence_tier."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "Pre-materialized intersection of sam_ppp_borrower_lance (name+state) "
            "and sam_ppp_address_lance (address physical+mailing). Every row is "
            "a (SAM UEI ↔ PPP borrower) pair where BOTH name and address agreed — "
            "the highest-precision SAM × PPP identity cohort available without "
            "additional sources."
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
        "bridge: %s  method=%s v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER,
    )
    logger.info("inputs: sam_ppp_borrower_lance + sam_ppp_address_lance")
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
        name_arrow, addr_arrow, rows_name, rows_addr = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            name_arrow, addr_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("intersection tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (composite):   %d", counts["rows_tier1"])
        logger.info("    gold     (composite):   %d", counts["rows_tier2"])
        logger.info("    silver   (composite):   %d", counts["rows_tier3"])
        logger.info("  by address_match_paths:")
        logger.info("    physical only:          %d", counts["rows_path_physical_only"])
        logger.info("    mailing only:           %d", counts["rows_path_mailing_only"])
        logger.info("    both:                   %d", counts["rows_path_both"])

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
                "rows_left": rows_name,
                "rows_right": rows_addr,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_path_physical_only": counts["rows_path_physical_only"],
                "rows_path_mailing_only": counts["rows_path_mailing_only"],
                "rows_path_both": counts["rows_path_both"],
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
