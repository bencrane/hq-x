#!/usr/bin/env python3
"""DuckDB bridge generator: PDL Free Company × SBA borrower (Lance edition).

*** REWRITTEN 2026-05-12 — inputs + output now read/write Lance datasets ***
*** Previous version wrote r2://dex-raw-landing-zone/bridges/pdl_sba_borrower/ ***
*** This version reads Lance and writes Lance per the sba-bridges-to-lance cycle ***

Reads:
  PDL: `polaris-warehouse/pdl/free_companies_lance/` (written by emit_pdl_free_companies_lance.py)
  SBA: `polaris-warehouse/sba/borrowers_lance/` (written by emit_sba_borrowers_lance.py)

Arrow-bridge pattern (NOT the lance-duckdb extension):
  lance.dataset(...).scanner(columns=[...]).to_table() → DuckDB register → SQL join.
  Reason: lance-duckdb extension is unstable on macOS arm64 per Lance canary
  cycle report 2026-05-12.

Preserves from the parquet version:
  - company_name_state_exact / pdl_sba_borrower registry calls
  - Fan-out tiering: platinum (1:1) / gold (1:N or N:1) / silver (N:M ≤50) / rejected
  - ops.bridge_generation_runs audit row per --apply invocation
  - bridge_run_id column embedded per bridge row

Output: `polaris-warehouse/bridges/pdl_sba_borrower_lance/`

Match method: `company_name_state_exact` v1.0.0
  JOIN on (legal_name_normalized, state) — same normalizer as emit scripts.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_pdl_sba_borrower.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_pdl_sba_borrower.py --dry-run
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

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
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
logger = logging.getLogger("build_bridge_pdl_sba_borrower")

# Bridge identity
BRIDGE_NAME = "pdl_sba_borrower"
METHOD_NAME = "company_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"  # bumped for Lance transition

SOURCE_LEFT = "pdl_free_companies_lance"
SOURCE_RIGHT = "sba_borrowers_lance"

# Lance I/O
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/"
SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_sba_borrower_lance/"
DATASET_SLUG = "pdl_sba_borrower_lance"

# Tier thresholds
COLLISION_THRESHOLD = 50  # >50 fan-out → rejected

# Row floor (tightened per directive §"Volume floors")
MIN_ROWS_MATCHED = 1_800_000  # borrower-level pairs (new Lance bridge); old parquet was 2.2M at loan-level

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


def _materialize_inputs(storage_options: dict) -> tuple[int, int]:
    """Read Lance datasets via Arrow-bridge; return (rows_left, rows_right)."""
    import duckdb
    import lance

    logger.info("opening PDL free_companies_lance via pyarrow (Arrow-bridge) ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(columns=[
        "pdl_id", "legal_name_normalized", "state",
        "pdl_name", "pdl_website", "pdl_linkedin_url",
        "pdl_industry", "pdl_size", "pdl_founded", "pdl_locality",
    ]).to_table()
    rows_left = len(pdl_arrow)
    logger.info("  pdl_free_companies_lance: %d rows", rows_left)

    logger.info("opening SBA borrowers_lance via pyarrow (Arrow-bridge) ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_arrow = sba_ds.scanner(columns=[
        "legal_name_normalized", "borrstate",
    ]).to_table()
    rows_right = len(sba_arrow)
    logger.info("  sba_borrowers_lance: %d rows", rows_right)

    return pdl_arrow, sba_arrow, rows_left, rows_right


def _build_match_table(
    pdl_arrow,
    sba_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Compute fan-out tiers + JOIN via DuckDB; return (con, counts_dict)."""
    import duckdb

    con = duckdb.connect()
    con.register("pdl_branded", pdl_arrow)
    con.register("sba_branded", sba_arrow)

    # Filter to valid join keys
    con.execute("""
        CREATE TEMP TABLE pdl_clean AS
        SELECT * FROM pdl_branded
        WHERE legal_name_normalized IS NOT NULL AND state IS NOT NULL
    """)
    con.execute("""
        CREATE TEMP TABLE sba_clean AS
        SELECT * FROM sba_branded
        WHERE legal_name_normalized IS NOT NULL AND borrstate IS NOT NULL
    """)

    logger.info("computing fan-out tables ...")
    con.execute("""
        CREATE TEMP TABLE pdl_fanout AS
        SELECT legal_name_normalized AS norm_name, state,
               COUNT(*) AS pdl_companies_at_name_state
        FROM pdl_clean
        GROUP BY legal_name_normalized, state
    """)
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT legal_name_normalized AS norm_name, borrstate AS state,
               COUNT(*) AS sba_borrowers_at_name_state
        FROM sba_clean
        GROUP BY legal_name_normalized, borrstate
    """)

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            p.legal_name_normalized     AS match_value_normalized,
            p.state                     AS match_state,
            s.legal_name_normalized     AS sba_name_normalized,
            s.borrstate                 AS sba_state,
            p.pdl_id,
            '{METHOD_NAME}'             AS match_method,
            sf.sba_borrowers_at_name_state AS sba_fan_out,
            pf.pdl_companies_at_name_state AS pdl_fan_out,
            CASE
                WHEN sf.sba_borrowers_at_name_state > {COLLISION_THRESHOLD}
                  OR pf.pdl_companies_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_borrowers_at_name_state = 1
                  AND pf.pdl_companies_at_name_state = 1
                    THEN 'platinum'
                WHEN sf.sba_borrowers_at_name_state = 1
                  OR pf.pdl_companies_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END                         AS confidence_tier,
            p.pdl_name,
            p.pdl_website,
            p.pdl_linkedin_url,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_founded,
            p.pdl_locality,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}'          AS bridge_version,
            '{bridge_run_id}'           AS bridge_run_id
        FROM pdl_clean p
        JOIN sba_clean s
          ON s.legal_name_normalized = p.legal_name_normalized
         AND s.borrstate = p.state
        JOIN pdl_fanout pf
          ON pf.norm_name = p.legal_name_normalized AND pf.state = p.state
        JOIN sba_fanout sf
          ON sf.norm_name = s.legal_name_normalized AND sf.state = s.borrstate
        """
    )
    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            bridge_run_id, confidence_tier,
            match_value_normalized, match_state,
            pdl_id, sba_name_normalized, sba_state,
            sba_fan_out, pdl_fan_out,
            pdl_name, pdl_website, pdl_linkedin_url,
            pdl_industry, pdl_size, pdl_founded, pdl_locality,
            generated_at, bridge_version
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT
            COUNT(*) AS rows_matched,
            COUNT(*) FILTER (WHERE confidence_tier='platinum') AS rows_tier1,
            COUNT(*) FILTER (WHERE confidence_tier='gold') AS rows_tier2,
            COUNT(*) FILTER (WHERE confidence_tier='silver') AS rows_tier3
        FROM bridge_match
    """).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match table to Lance; return row count."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        try:
            ds.create_scalar_index("match_value_normalized", index_type="BTREE", replace=True)
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)

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
    """Idempotent UPSERTs: company_name_state_exact + pdl_sba_borrower."""
    logger.info("registering match_method + bridge in ops registry ...")
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state) "
            "applying _lib/entity_name_normalize.py v1.0.0."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1 at (name,state); gold=1:N or N:1; "
            "silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_name_normalized", "state"],
        input_columns_right=["legal_name_normalized", "borrstate"],
        output_value_description="normalized name + 2-letter state join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "PDL Free Companies × SBA borrower (Lance edition). "
            "Provides PDL website + LinkedIn + industry + size + founded. "
            "Lance-rewrite of the 2026-05-10 parquet bridge."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s", NORMALIZER_VERSION)
    logger.info("inputs: PDL Lance + SBA borrowers Lance (Arrow-bridge)")
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
        pdl_arrow, sba_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            pdl_arrow, sba_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info("    silver   (N:M ≤%d):     %d", COLLISION_THRESHOLD, counts["rows_tier3"])
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
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
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
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info("OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0)
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
