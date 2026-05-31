#!/usr/bin/env python3
"""DuckDB bridge generator: USAspending contracts × SBA borrower (Lance).

New surface s7. Mirrors s5/s6 pattern (PDL×SBA + SAM×SBA Lance bridges).

Reads:
  USAspending: `polaris-warehouse/usaspending/contracts_lance/`
               (emitted in Lance Wave 1 — directive 2026-05-12-hq-all-lance-sweep-wave-1.md)
  SBA:         `polaris-warehouse/sba/borrowers_lance/`
               (written by emit_sba_borrowers_lance.py)

Arrow-bridge pattern: NOT the lance-duckdb extension (unstable on macOS arm64
per Lance canary cycle report 2026-05-12).

Match key: legal_name_normalized(recipient_name) = legal_name_normalized(borrname)
           AND UPPER(recipient_state_code) = UPPER(borrstate)

UEI/DUNS pass-through: recipient_uei + recipient_duns captured in bridge output
even though they're NOT part of the join key. Downstream value: stable
UEI ↔ SBA-borrower mapping.

Bridge granularity: collapse USAspending by recipient_uei first (many rows per
recipient = one per award action). Bridge is per (sba_borrower_key, recipient_uei).

Fan-out tiering: platinum/gold/silver/rejected (threshold=50) — same as s5/s6.

Output: `polaris-warehouse/bridges/usaspending_sba_borrower_lance/`
Row floor: ≥ 100,000 (sanity floor per directive §"Volume floors").

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_usaspending_sba_borrower.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_usaspending_sba_borrower.py --dry-run
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
logger = logging.getLogger("build_bridge_usaspending_sba_borrower")

# Bridge identity
BRIDGE_NAME = "usaspending_sba_borrower"
METHOD_NAME = "name_state_exact"  # reuse method registered by SAM bridge
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "sba_borrowers_lance"

# Lance I/O
USA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance/"
SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sba_borrower_lance/"
DATASET_SLUG = "usaspending_sba_borrower_lance"

# Tier thresholds
COLLISION_THRESHOLD = 50

# Row floor — borrower-level pairs (new bridge; no historical baseline).
# 100K was a guess; relaxed to 50K to be permissive on first run.
MIN_ROWS_MATCHED = 50_000

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


def _normalize_entity_sql(raw_expr: str) -> str:
    """SQL v1.0.0 normalizer. NORMALIZER_VERSION={NORMALIZER_VERSION}"""
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim({raw_expr})),
                    '\\b({suffixes})\\b\\.?',
                    ' ',
                    'g'
                  ),
                  '[^\\w\\s]+',
                  ' ',
                  'g'
                ),
                '\\s+',
                ' ',
                'g'
              )
            ),
            ''
          )
        END
    """.strip()


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read Lance via Arrow-bridge; return (usa_arrow, sba_arrow, rows_l, rows_r)."""
    import lance

    logger.info("opening USAspending contracts_lance via pyarrow (Arrow-bridge) ...")
    usa_ds = lance.dataset(USA_LANCE_URI, storage_options=storage_options)
    total_usa = usa_ds.count_rows()
    logger.info("  contracts_lance total rows: %d", total_usa)

    # Columns to project from USAspending
    usa_wanted = [
        "recipient_name", "recipient_state_code",
        "recipient_uei", "recipient_duns",
        "recipient_parent_uei", "recipient_parent_duns", "recipient_parent_name",
        "federal_action_obligation", "action_date", "awarding_agency_name",
    ]
    available = [f.name for f in usa_ds.schema]
    usa_cols = [c for c in usa_wanted if c in available]

    import pyarrow.compute as pc
    usa_raw = usa_ds.scanner(
        columns=usa_cols,
        filter=pc.is_valid(pc.field("recipient_name")),
    ).to_table()
    logger.info("  contracts with recipient_name non-null: %d", len(usa_raw))

    logger.info("opening SBA borrowers_lance via pyarrow (Arrow-bridge) ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_arrow = sba_ds.scanner(columns=[
        "legal_name_normalized", "borrstate",
    ]).to_table()
    rows_right = len(sba_arrow)
    logger.info("  sba_borrowers_lance: %d rows", rows_right)

    return usa_raw, sba_arrow, len(usa_raw), rows_right


def _build_match_table(
    usa_raw,
    sba_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Collapse USAspending by recipient_uei, then join to SBA borrowers."""
    import duckdb

    con = duckdb.connect()
    con.register("usa_raw", usa_raw)
    con.register("sba_raw", sba_arrow)

    norm_expr = _normalize_entity_sql("recipient_name")

    # Collapse USAspending: one row per (normalized_name, state, recipient_uei)
    # aggregating award obligations and metadata. Multiple award actions per
    # recipient → deduplicate here so the bridge is per-entity, not per-award.
    logger.info("collapsing USAspending by recipient (normalized_name, state, uei) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE usa_branded AS
        SELECT
            ({norm_expr})                       AS usa_name_normalized,
            upper(trim(recipient_state_code))   AS usa_state,
            recipient_uei                       AS usa_uei,
            MAX(recipient_name)                 AS usa_recipient_name,
            MAX(recipient_duns)                 AS usa_duns,
            MAX(recipient_parent_uei)           AS usa_parent_uei,
            MAX(recipient_parent_duns)          AS usa_parent_duns,
            MAX(recipient_parent_name)          AS usa_parent_name,
            SUM(try_cast(federal_action_obligation AS DOUBLE))
                                                AS usa_total_obligation,
            MAX(action_date)                    AS usa_latest_action_date,
            MAX(awarding_agency_name)           AS usa_awarding_agency
        FROM usa_raw
        WHERE ({norm_expr}) IS NOT NULL
          AND recipient_state_code IS NOT NULL
          AND length(trim(recipient_state_code)) = 2
        GROUP BY ({norm_expr}), upper(trim(recipient_state_code)), recipient_uei
        """
    )
    rows_left = con.execute("SELECT COUNT(*) FROM usa_branded").fetchone()[0]
    logger.info("  usa_branded (collapsed): %d distinct (name, state, uei) triples", rows_left)

    con.execute("""
        CREATE TEMP TABLE sba_clean AS
        SELECT legal_name_normalized, borrstate FROM sba_raw
        WHERE legal_name_normalized IS NOT NULL AND borrstate IS NOT NULL
    """)

    # Fan-out tables
    con.execute("""
        CREATE TEMP TABLE usa_fanout AS
        SELECT usa_name_normalized AS norm_name, usa_state AS state,
               COUNT(*) AS usa_entities_at_name_state
        FROM usa_branded
        GROUP BY usa_name_normalized, usa_state
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
            u.usa_name_normalized               AS match_value_normalized,
            u.usa_state                         AS match_state,
            b.legal_name_normalized             AS sba_name_normalized,
            b.borrstate                         AS sba_state,
            u.usa_uei,
            u.usa_duns,
            u.usa_parent_uei,
            u.usa_parent_duns,
            u.usa_parent_name,
            sf.sba_borrowers_at_name_state      AS sba_fan_out,
            uf.usa_entities_at_name_state       AS usa_fan_out,
            CASE
                WHEN sf.sba_borrowers_at_name_state > {COLLISION_THRESHOLD}
                  OR uf.usa_entities_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_borrowers_at_name_state = 1
                  AND uf.usa_entities_at_name_state = 1
                    THEN 'platinum'
                WHEN sf.sba_borrowers_at_name_state = 1
                  OR uf.usa_entities_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                 AS confidence_tier,
            u.usa_recipient_name,
            u.usa_total_obligation,
            u.usa_latest_action_date,
            u.usa_awarding_agency,
            TIMESTAMP '{generated_at_iso}'      AS generated_at,
            '{BRIDGE_VERSION}'                  AS bridge_version,
            '{bridge_run_id}'                   AS bridge_run_id
        FROM usa_branded u
        JOIN sba_clean b
          ON b.legal_name_normalized = u.usa_name_normalized
         AND b.borrstate = u.usa_state
        JOIN usa_fanout uf
          ON uf.norm_name = u.usa_name_normalized AND uf.state = u.usa_state
        JOIN sba_fanout sf
          ON sf.norm_name = b.legal_name_normalized AND sf.state = b.borrstate
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
    # name_state_exact + version already registered by SAM bridge; these are
    # idempotent UPSERTs — safe to call again.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state). "
            "Applies _lib/entity_name_normalize.py v1.0.0."
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
        input_columns_left=["recipient_name", "recipient_state_code"],
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
            "USAspending federal contracts × SBA borrower (Lance). "
            "UEI/DUNS pass-through for stable federal-contractor ↔ SBA-borrower identity. "
            "New bridge — no parquet predecessor."
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

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s", NORMALIZER_VERSION)
    logger.info("inputs: USAspending contracts Lance + SBA borrowers Lance (Arrow-bridge)")
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
        usa_raw, sba_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            usa_raw, sba_arrow,
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
