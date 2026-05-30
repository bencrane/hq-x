#!/usr/bin/env python3
"""SBIC × CO UCC secured parties via name-stem prefix (Pattern B Lance).

CO parity port of build_bridge_sbic_ucc_ca_secured_party_lance.py (CA edition).

Identifies which SBIC fund-manager firms appear as secured parties on
Colorado UCC-1 filings — i.e. SBICs with active CO-collateral lending
positions. Catches both:
  - direct: SBIC manager firm IS the secured party (verbatim on a CO UCC-1)
  - sibling: SBIC and UCC entity share a brand-stem

UCC filing state is CO (collateral is CO-domiciled), but the secured party
(lender) can be anywhere. The match is name-only, not state-filtered — the
cohort of interest is ALL SBICs with active CO lending, not just CO-HQ'd.

Pattern B per inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md:
  - inputs: sba.sbic_directory_lance + ucc_co.secured_parties_lance
  - method: company_name_stem_prefix v1.0.0 (REUSED — do NOT re-register
    method/version; only register_bridge)
  - normalizer: _lib/entity_name_normalize.py v1.0.0 on both sides
  - UCC filter: SECURED_PARTY_TYPE = 'Organization'
  - UCC dedupe: GROUP BY normalized ORG_NAME, aggregate filing counts +
    state/city distinct sets

Floor: 1 (exploratory; some SBIC managers will appear, some won't).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sbic_ucc_co_secured_party_lance.py --apply
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

from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sbic_ucc_co_secured_party_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sbic_ucc_co_secured_party_lance"
METHOD_NAME = "company_name_stem_prefix"  # reused — register_bridge only
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_sbic_directory_lance"
SOURCE_RIGHT = "ucc_co_secured_parties_lance"

# R2 layout ------------------------------------------------------------------
SBIC_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/sbic_directory_lance"
)
UCC_CO_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/secured_parties_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
    "sbic_ucc_co_secured_party_lance"
)
DATASET_SLUG = "sbic_ucc_co_secured_party_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 1
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
    import lance
    import pyarrow.compute as pc

    logger.info("opening sba/sbic_directory_lance ...")
    sbic_ds = lance.dataset(SBIC_LANCE_URI, storage_options=storage_options)
    sbic_arrow = sbic_ds.scanner(
        columns=[
            "fund_name", "fund_name_normalized",
            "manager", "manager_name_normalized",
            "state", "city",
            "vintage_year", "fund_size_usd",
            "average_investment_usd",
            "investment_strategy", "fund_style",
            "making_new_investments",
        ],
        filter=pc.field("manager_name_normalized").is_valid(),
    ).to_table()
    rows_sbic = len(sbic_arrow)
    logger.info("  sbic funds: %d rows", rows_sbic)

    logger.info("opening ucc_co/secured_parties_lance ...")
    ucc_ds = lance.dataset(UCC_CO_LANCE_URI, storage_options=storage_options)
    ucc_arrow = ucc_ds.scanner(
        columns=[
            "UCC1_NUM", "SECURED_PARTY_TYPE",
            "ORG_NAME",
            "ADDR1", "CITY", "STATE", "POSTAL_CODE",
        ],
        filter=(pc.field("SECURED_PARTY_TYPE") == "Organization")
               & pc.field("ORG_NAME").is_valid(),
    ).to_table()
    rows_ucc = len(ucc_arrow)
    logger.info("  ucc_co organization secured-party rows: %d", rows_ucc)

    return sbic_arrow, ucc_arrow, rows_sbic, rows_ucc


def _build_match_table(
    sbic_arrow,
    ucc_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("sbic_raw", sbic_arrow)
    con.register("ucc_raw", ucc_arrow)

    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )

    logger.info("deduping sbic by manager_name_normalized ...")
    con.execute(
        """
        CREATE TEMP TABLE sbic_managers AS
        SELECT
            manager_name_normalized,
            any_value(manager) AS manager,
            any_value(state) AS manager_state,
            any_value(city) AS manager_city,
            count(*) AS sbic_funds_under_manager,
            string_agg(DISTINCT fund_name, ' | ') AS sbic_fund_names,
            min(vintage_year) AS earliest_vintage,
            max(vintage_year) AS latest_vintage,
            sum(fund_size_usd) AS total_fund_size_usd,
            bool_or(making_new_investments) AS any_fund_making_new_invest
        FROM sbic_raw
        WHERE manager_name_normalized IS NOT NULL
          AND length(manager_name_normalized) >= 3
        GROUP BY manager_name_normalized
        """
    )
    rows_sbic_mgr = con.execute(
        "SELECT count(*) FROM sbic_managers"
    ).fetchone()[0]
    logger.info("  distinct SBIC manager firms: %d", rows_sbic_mgr)

    logger.info("aggregating ucc_co secured parties by normalized ORG_NAME ...")
    con.execute(
        """
        CREATE TEMP TABLE ucc_secured_parties AS
        SELECT
            py_normalize_entity(ORG_NAME) AS org_name_normalized,
            any_value(ORG_NAME) AS org_name,
            count(*) AS ucc_filing_count,
            count(DISTINCT UCC1_NUM) AS distinct_ucc1_count,
            string_agg(DISTINCT STATE, ',') AS sp_states,
            string_agg(DISTINCT CITY,  ',' ORDER BY CITY) FILTER (
                WHERE CITY IS NOT NULL
            ) AS sp_cities
        FROM ucc_raw
        WHERE ORG_NAME IS NOT NULL AND trim(ORG_NAME) != ''
        GROUP BY py_normalize_entity(ORG_NAME)
        HAVING py_normalize_entity(ORG_NAME) IS NOT NULL
           AND length(py_normalize_entity(ORG_NAME)) >= 3
        """
    )
    rows_ucc_sp = con.execute(
        "SELECT count(*) FROM ucc_secured_parties"
    ).fetchone()[0]
    logger.info("  distinct UCC secured-party orgs (normalized): %d", rows_ucc_sp)

    logger.info("stem-prefix joining + fan-out ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_pairs AS
        SELECT
            s.manager_name_normalized,
            s.manager,
            s.manager_state,
            s.manager_city,
            s.sbic_funds_under_manager,
            s.sbic_fund_names,
            s.earliest_vintage,
            s.latest_vintage,
            s.total_fund_size_usd,
            s.any_fund_making_new_invest,
            u.org_name_normalized,
            u.org_name,
            u.ucc_filing_count,
            u.distinct_ucc1_count,
            u.sp_states,
            u.sp_cities,
            CASE
                WHEN length(s.manager_name_normalized)
                   <= length(u.org_name_normalized)
                    THEN s.manager_name_normalized
                ELSE u.org_name_normalized
            END AS match_value
        FROM sbic_managers s
        JOIN ucc_secured_parties u
          ON s.manager_name_normalized = u.org_name_normalized
          OR u.org_name_normalized LIKE s.manager_name_normalized || ' %'
          OR s.manager_name_normalized LIKE u.org_name_normalized || ' %'
        """
    )
    raw_pairs = con.execute(
        "SELECT count(*) FROM bridge_pairs"
    ).fetchone()[0]
    logger.info("  raw matched pairs (pre-tier): %d", raw_pairs)

    con.execute(
        """
        CREATE TEMP TABLE sbic_fanout AS
        SELECT match_value,
               count(DISTINCT manager_name_normalized) AS sbic_count_at_stem
        FROM bridge_pairs
        GROUP BY match_value
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT match_value,
               count(DISTINCT org_name_normalized) AS ucc_count_at_stem
        FROM bridge_pairs
        GROUP BY match_value
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            p.*,
            '{METHOD_NAME}' AS match_method,
            sf.sbic_count_at_stem,
            uf.ucc_count_at_stem,
            CASE
                WHEN sf.sbic_count_at_stem > {COLLISION_THRESHOLD}
                  OR uf.ucc_count_at_stem > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sbic_count_at_stem = 1
                  AND uf.ucc_count_at_stem = 1
                    THEN 'platinum'
                WHEN sf.sbic_count_at_stem = 1
                  OR uf.ucc_count_at_stem = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM bridge_pairs p
        JOIN sbic_fanout sf USING (match_value)
        JOIN ucc_fanout  uf USING (match_value)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT *
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    row_counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT count(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    return con, {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
        "sbic_managers": rows_sbic_mgr,
        "ucc_secured_parties": rows_ucc_sp,
    }


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=10_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)",
                    lance_count, time.time() - t0, ds.version)

        if lance_count > 0:
            for col in ("manager_name_normalized", "match_value"):
                try:
                    ds.create_scalar_index(
                        col, index_type="BTREE", replace=True
                    )
                    logger.info("  BTREE on %s: OK", col)
                except Exception as e:
                    logger.warning("BTREE on %s non-fatal: %s", col, e)
        else:
            logger.info("  skipping BTREE (0 rows)")
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files non-fatal: %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions non-fatal: %s", e)

    return lance_count


def _ensure_registry() -> None:
    """register_bridge ONLY — company_name_stem_prefix v1.0.0 already registered
    by the SBIC × SBA 7(a) lender bridge. Per the method-reuse precedent, reuse
    the shared method; do NOT call register_match_method_version (would clobber
    the original input_columns provenance).
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA SBIC Directory × CO UCC secured parties by name-stem "
            "prefix. Identifies SBIC fund-managers active as secured "
            "parties on Colorado UCC-1 filings (CO-collateral lending "
            "positions). Captures both exact name match and brand-stem "
            "sibling links. CO parity port of the sbic_ucc_ca_secured_party "
            "bridge; reuses company_name_stem_prefix v1.0.0."
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
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)",
                BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("inputs: %s + %s", SBIC_LANCE_URI, UCC_CO_LANCE_URI)
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
        sbic_arrow, ucc_arrow, rows_sbic, rows_ucc = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sbic_arrow,
            ucc_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:           %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (stem 1:1):  %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (1:N | N:1): %s", f"{counts['rows_tier2']:,}")
        logger.info(
            "    silver   (N:M <=%d):  %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected: %s",
            f"{counts['rows_collision_rejected']:,}",
        )

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
            logger.info("DRY RUN — no writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": counts["sbic_managers"],
                "rows_right": counts["ucc_secured_parties"],
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,
            },
        )
        logger.info(
            "OK — run_id=%s lance_rows=%d duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
