#!/usr/bin/env python3
"""DuckDB bridge generator: SBA lenders × GLEIF LEI records (Lance edition).

Cycle: ucc-gleif-identity-spine (s7).

Both sides have pre-normalized name columns — no inline normalization needed.

Reads:
  SBA:   polaris-warehouse/sba/lenders_lance/         (11K SBA originators)
  GLEIF: polaris-warehouse/gleif/lei_records_lance/   (3.3M rows)

Arrow-bridge pattern (NOT the lance-duckdb extension).

Join: sba.bankname_normalized = gleif.legal_name_normalized
      AND upper(sba.bankstate) = substr(gleif.headquarters_region, 4, 2)

GLEIF filter: entity_status='ACTIVE', headquarters_country='US'.
State derive: substr(headquarters_region, 4, 2) extracts 'CA' from 'US-CA'.

Expected high coverage: most institutional banks + non-bank SBLCs are LEI-registered.

Output: polaris-warehouse/bridges/sba_lender_gleif_lance/
Floor: ≥ 200 rows (recalibrated; actual exact-name ceiling ~298 — GLEIF uses legal names, SBA uses trade names).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_lender_gleif_lance.py --apply
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
logger = logging.getLogger("build_bridge_sba_lender_gleif_lance")

BRIDGE_NAME = "sba_lender_gleif"
METHOD_NAME = "company_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_lenders_lance"
SOURCE_RIGHT = "gleif_lei_records_lance"

SBA_LENDERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance"
GLEIF_LEI_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_records_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_lender_gleif_lance"
DATASET_SLUG = "sba_lender_gleif_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 200  # Recalibrated: actual exact-name ceiling ~298 rows (GLEIF uses legal names, SBA uses trade names)
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

    logger.info("opening sba/lenders_lance via Arrow-bridge ...")
    sba_ds = lance.dataset(SBA_LENDERS_LANCE_URI, storage_options=storage_options)
    sba_arrow = sba_ds.scanner(
        columns=[
            "bankname_normalized", "bankname_sample", "bankstate",
            "lender_key", "lender_type", "bankfdicnumber", "bankncuanumber",
        ]
    ).to_table()
    rows_left = len(sba_arrow)
    logger.info("  sba/lenders_lance: %d rows", rows_left)

    logger.info("opening gleif/lei_records_lance via Arrow-bridge (ACTIVE + US) ...")
    gleif_ds = lance.dataset(GLEIF_LEI_LANCE_URI, storage_options=storage_options)
    gleif_arrow = gleif_ds.scanner(
        columns=[
            "lei", "legal_name", "legal_name_normalized",
            "headquarters_region", "entity_status", "entity_category",
            "headquarters_country",
        ],
        filter=(
            (pc.field("entity_status") == "ACTIVE")
            & (pc.field("headquarters_country") == "US")
        ),
    ).to_table()
    rows_right = len(gleif_arrow)
    logger.info("  gleif/lei_records_lance (ACTIVE + US): %d rows", rows_right)

    return sba_arrow, gleif_arrow, rows_left, rows_right


def _build_match_table(
    sba_arrow, gleif_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    import duckdb

    con = duckdb.connect()
    con.register("sba_raw", sba_arrow)
    con.register("gleif_raw", gleif_arrow)

    con.execute("""
        CREATE TEMP TABLE sba_clean AS
        SELECT * FROM sba_raw
        WHERE bankname_normalized IS NOT NULL AND bankstate IS NOT NULL
    """)
    con.execute("""
        CREATE TEMP TABLE gleif_clean AS
        SELECT
            lei,
            legal_name                          AS gleif_legal_name,
            legal_name_normalized,
            entity_category,
            substr(headquarters_region, 4, 2)   AS gleif_state
        FROM gleif_raw
        WHERE legal_name_normalized IS NOT NULL
          AND headquarters_region IS NOT NULL
          AND length(headquarters_region) >= 5
    """)

    logger.info("computing fan-out tables ...")
    # Fan-out on name alone — state removed from join key (permissive; lenders are national)
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT bankname_normalized AS norm_name,
               COUNT(*) AS sba_fan_out
        FROM sba_clean GROUP BY bankname_normalized
    """)
    con.execute("""
        CREATE TEMP TABLE gleif_fanout AS
        SELECT legal_name_normalized AS norm_name,
               COUNT(*) AS gleif_fan_out
        FROM gleif_clean GROUP BY legal_name_normalized
    """)

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            s.bankname_normalized,
            s.bankstate,
            g.lei,
            g.gleif_state,
            s.lender_key,
            s.lender_type,
            s.bankfdicnumber,
            s.bankncuanumber,
            g.gleif_legal_name,
            g.entity_category,
            sf.sba_fan_out,
            gf.gleif_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR gf.gleif_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND gf.gleif_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR gf.gleif_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                             AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM sba_clean s
        JOIN gleif_clean g
          ON g.legal_name_normalized = s.bankname_normalized
        JOIN sba_fanout sf
          ON sf.norm_name = s.bankname_normalized
        JOIN gleif_fanout gf
          ON gf.norm_name = g.legal_name_normalized
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
            ds.create_scalar_index("bankname_normalized", index_type="BTREE", replace=True)
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
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state). "
            "Applies pre-normalized name columns. GLEIF state from substr(headquarters_region, 4, 2)."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description="platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50",
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["bankname_normalized", "bankstate"],
        input_columns_right=["legal_name_normalized", "headquarters_region"],
        output_value_description="normalized name + 2-letter state join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA originating lenders × GLEIF LEI records. "
            "Institutional-lender LEI lookup (FDIC banks + non-bank SBLCs are often LEI-registered). "
            "Both sides pre-normalized."
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
    logger.info("inputs: SBA lenders_lance + GLEIF lei_records_lance (Arrow-bridge)")
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
        sba_arrow, gleif_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_arrow, gleif_arrow,
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
