#!/usr/bin/env python3
"""DuckDB bridge generator: UCC CO canonical lenders × SBA lenders (Lance edition).

CO parity port of build_bridge_ucc_sba_lender_lance.py (the CA edition).

Filter-layer bridge. Both sides ALREADY have pre-normalized name columns.
No inline normalization needed.

Reads:
  UCC: polaris-warehouse/ucc_co/lenders_lance/   (58,840 canonical UCC lenders)
  SBA: polaris-warehouse/sba/lenders_lance/       (11K SBA originators)

Arrow-bridge pattern (NOT the lance-duckdb extension).

Join: ucc.lender_name_normalized = sba.bankname_normalized (no state filter —
lenders operate nationally).

This is the FILTER layer — the intersection identifies UCC-active secured parties
that are also SBA originators (competitors). The complement is the actionable
demand-side cohort.

Method company_name_exact v1.0.0 is REUSED (registered by the CA ucc_sba_lender
bridge). Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md, a bridge reusing an existing
method calls only register_bridge().

Output: polaris-warehouse/bridges/ucc_co_sba_lender_lance/
Floor: MIN_ROWS_MATCHED — calibrated from --dry-run.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ucc_co_sba_lender_lance.py --apply
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
logger = logging.getLogger("build_bridge_ucc_co_sba_lender_lance")

BRIDGE_NAME = "ucc_co_sba_lender"
METHOD_NAME = "company_name_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "ucc_co_lenders_lance"
SOURCE_RIGHT = "sba_lenders_lance"

UCC_LENDERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/lenders_lance"
SBA_LENDERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_co_sba_lender_lance"
DATASET_SLUG = "ucc_co_sba_lender_lance"

COLLISION_THRESHOLD = 50
# Calibrated from --dry-run: natural matched-row count 3,361 (375 platinum
# + 2,986 gold; 0 silver, 0 collision-rejected). Floor set conservatively
# below it as a regression tripwire.
MIN_ROWS_MATCHED = 2_000
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

    logger.info("opening ucc_co/lenders_lance via Arrow-bridge ...")
    ucc_ds = lance.dataset(UCC_LENDERS_LANCE_URI, storage_options=storage_options)
    ucc_arrow = ucc_ds.scanner(
        columns=[
            "lender_name_normalized", "total_filings", "active_filings",
            "bank_classification", "category_inferred_from_name",
        ]
    ).to_table()
    rows_left = len(ucc_arrow)
    logger.info("  ucc_co/lenders_lance: %d rows", rows_left)

    logger.info("opening sba/lenders_lance via Arrow-bridge ...")
    sba_ds = lance.dataset(SBA_LENDERS_LANCE_URI, storage_options=storage_options)
    sba_arrow = sba_ds.scanner(
        columns=[
            "bankname_normalized", "bankname_sample", "bankstate",
            "lender_key", "lender_type", "bankfdicnumber", "bankncuanumber",
            "total_loans", "total_originated_dollars",
        ]
    ).to_table()
    rows_right = len(sba_arrow)
    logger.info("  sba/lenders_lance: %d rows", rows_right)

    return ucc_arrow, sba_arrow, rows_left, rows_right


def _build_match_table(
    ucc_arrow, sba_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    import duckdb

    con = duckdb.connect()
    con.register("ucc_raw", ucc_arrow)
    con.register("sba_raw", sba_arrow)

    con.execute("""
        CREATE TEMP TABLE ucc_clean AS
        SELECT *,
               lower(lender_name_normalized) AS lender_name_normalized_lc
        FROM ucc_raw
        WHERE lender_name_normalized IS NOT NULL
    """)
    con.execute("""
        CREATE TEMP TABLE sba_clean AS
        SELECT * FROM sba_raw
        WHERE bankname_normalized IS NOT NULL
    """)

    logger.info("computing fan-out tables ...")
    # Fan-out on lowercase normalized name (UCC stored uppercase; SBA stored lowercase)
    con.execute("""
        CREATE TEMP TABLE ucc_fanout AS
        SELECT lender_name_normalized_lc AS norm_name, COUNT(*) AS ucc_fan_out
        FROM ucc_clean GROUP BY lender_name_normalized_lc
    """)
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT bankname_normalized AS norm_name, COUNT(*) AS sba_fan_out
        FROM sba_clean GROUP BY bankname_normalized
    """)

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            u.lender_name_normalized,
            s.bankname_normalized,
            s.lender_key,
            s.lender_type,
            s.bankstate,
            s.bankfdicnumber,
            s.bankncuanumber,
            u.bank_classification,
            u.category_inferred_from_name,
            u.total_filings                     AS total_filings_ucc,
            s.total_loans                       AS total_loans_sba,
            s.total_originated_dollars          AS total_originated_dollars_sba,
            uf.ucc_fan_out,
            sf.sba_fan_out,
            CASE
                WHEN uf.ucc_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sba_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.ucc_fan_out = 1 AND sf.sba_fan_out = 1
                    THEN 'platinum'
                WHEN uf.ucc_fan_out = 1 OR sf.sba_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                 AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'      AS generated_at,
            '{BRIDGE_VERSION}'                  AS bridge_version,
            '{bridge_run_id}'                   AS bridge_run_id
        FROM ucc_clean u
        JOIN sba_clean s ON s.bankname_normalized = u.lender_name_normalized_lc
        JOIN ucc_fanout uf ON uf.norm_name = u.lender_name_normalized_lc
        JOIN sba_fanout sf ON sf.norm_name = s.bankname_normalized
        """
    )
    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver')
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
            ds.create_scalar_index("lender_name_normalized", index_type="BTREE", replace=True)
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
    # Method company_name_exact v1.0.0 is shared (registered by the CA
    # ucc_sba_lender bridge). Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md, a
    # bridge reusing an existing method calls only register_bridge().
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "UCC CO canonical lenders × SBA originating lenders. "
            "Both sides pre-normalized. Intersection = SBA-originator competitors. "
            "Complement is the actionable demand-side UCC cohort. "
            "CO parity port of the ucc_sba_lender (CA) bridge; reuses company_name_exact v1.0.0."
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
    logger.info("inputs: UCC CO lenders_lance + SBA lenders_lance (Arrow-bridge)")
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
        ucc_arrow, sba_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            ucc_arrow, sba_arrow,
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
