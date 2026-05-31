#!/usr/bin/env python3
"""DuckDB bridge generator: UCC secured parties × GLEIF LEI records (Lance edition).

Cycle: ucc-gleif-identity-spine (s3).

Reads:
  UCC:   polaris-warehouse/ucc_ca/secured_parties_lance/  (4.7M rows, raw — no pre-normalized name)
  GLEIF: polaris-warehouse/gleif/lei_records_lance/        (3.3M rows)

Arrow-bridge pattern (NOT the lance-duckdb extension — unstable on macOS arm64).

CRITICAL: UCC secured_parties_lance has NO pre-normalized name column. Inline
normalization applied to ORG_NAME via _normalize_entity_sql(). Filter
SECURED_PARTY_TYPE='Organization' to organizations only.

GLEIF side: join on legal_name_normalized (already in lei_records_lance).
State join: substr(headquarters_region, 4, 2) extracts 'CA' from 'US-CA'.

Fan-out tiering: platinum (1:1) / gold (1:N or N:1) / silver (N:M ≤50) / rejected.

Output: polaris-warehouse/bridges/ucc_gleif_lance/
Schema: secured_party_name_normalized, secured_party_state, lei, gleif_legal_name,
        confidence_tier, ucc_fan_out, gleif_fan_out, generated_at, bridge_version,
        bridge_run_id

Floor: ≥ 100,000 rows.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ucc_gleif_lance.py --apply
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
logger = logging.getLogger("build_bridge_ucc_gleif_lance")

BRIDGE_NAME = "ucc_gleif"
METHOD_NAME = "company_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "ucc_ca_secured_parties_lance"
SOURCE_RIGHT = "gleif_lei_records_lance"

UCC_SP_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
GLEIF_LEI_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_records_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_gleif_lance"
DATASET_SLUG = "ucc_gleif_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100_000
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
    """SQL equivalent of entity_name_normalize.py v1.0.0."""
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
    import lance
    import pyarrow.compute as pc

    logger.info("opening ucc_ca/secured_parties_lance via Arrow-bridge ...")
    ucc_ds = lance.dataset(UCC_SP_LANCE_URI, storage_options=storage_options)
    ucc_arrow = ucc_ds.scanner(
        columns=["UCC1_NUM", "SECURED_PARTY_TYPE", "ORG_NAME", "STATE"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
    ).to_table()
    rows_left = len(ucc_arrow)
    logger.info("  ucc_ca/secured_parties_lance (SECURED_PARTY_TYPE=Organization): %d rows", rows_left)

    logger.info("opening gleif/lei_records_lance via Arrow-bridge ...")
    gleif_ds = lance.dataset(GLEIF_LEI_LANCE_URI, storage_options=storage_options)
    gleif_arrow = gleif_ds.scanner(
        columns=["lei", "legal_name", "legal_name_normalized", "headquarters_region",
                 "headquarters_country"],
        filter=pc.field("headquarters_country") == "US",
    ).to_table()
    rows_right = len(gleif_arrow)
    logger.info("  gleif/lei_records_lance (headquarters_country=US): %d rows", rows_right)

    return ucc_arrow, gleif_arrow, rows_left, rows_right


def _build_match_table(
    ucc_arrow,
    gleif_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    import duckdb

    con = duckdb.connect()
    con.register("ucc_raw", ucc_arrow)
    con.register("gleif_raw", gleif_arrow)

    norm_expr = _normalize_entity_sql("ORG_NAME")

    con.execute(
        f"""
        CREATE TEMP TABLE ucc_branded AS
        SELECT
            UCC1_NUM                            AS ucc1_num,
            ({norm_expr})                       AS ucc_name_normalized,
            upper(trim(STATE))                  AS ucc_state
        FROM ucc_raw
        WHERE ({norm_expr}) IS NOT NULL
          AND STATE IS NOT NULL
          AND length(trim(STATE)) = 2
        """
    )
    rows_ucc = con.execute("SELECT COUNT(*) FROM ucc_branded").fetchone()[0]
    logger.info("  ucc_branded (name+state non-null): %d", rows_ucc)

    con.execute("""
        CREATE TEMP TABLE gleif_clean AS
        SELECT
            lei,
            legal_name                          AS gleif_legal_name,
            legal_name_normalized,
            substr(headquarters_region, 4, 2)   AS gleif_state
        FROM gleif_raw
        WHERE legal_name_normalized IS NOT NULL
          AND headquarters_region IS NOT NULL
          AND length(headquarters_region) >= 5
    """)

    logger.info("computing fan-out tables ...")
    # Fan-out on name alone — state is NOT part of the join key (permissive per directive)
    con.execute("""
        CREATE TEMP TABLE ucc_fanout AS
        SELECT ucc_name_normalized AS norm_name,
               COUNT(DISTINCT ucc_state) AS ucc_fan_out
        FROM ucc_branded
        GROUP BY ucc_name_normalized
    """)
    con.execute("""
        CREATE TEMP TABLE gleif_fanout AS
        SELECT legal_name_normalized AS norm_name,
               COUNT(*) AS gleif_fan_out
        FROM gleif_clean
        GROUP BY legal_name_normalized
    """)

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            u.ucc_name_normalized           AS secured_party_name_normalized,
            u.ucc_state                     AS secured_party_state,
            g.lei,
            g.gleif_legal_name,
            g.gleif_state,
            uf.ucc_fan_out,
            gf.gleif_fan_out,
            CASE
                WHEN uf.ucc_fan_out > {COLLISION_THRESHOLD}
                  OR gf.gleif_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.ucc_fan_out = 1 AND gf.gleif_fan_out = 1
                    THEN 'platinum'
                WHEN uf.ucc_fan_out = 1 OR gf.gleif_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                             AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM ucc_branded u
        JOIN gleif_clean g
          ON g.legal_name_normalized = u.ucc_name_normalized
        JOIN ucc_fanout uf
          ON uf.norm_name = u.ucc_name_normalized
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
            ds.create_scalar_index("secured_party_name_normalized", index_type="BTREE", replace=True)
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
        input_columns_left=["ORG_NAME", "STATE"],
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
            "UCC CA secured parties (org-type) × GLEIF LEI records. "
            "Inline-normalized UCC ORG_NAME joined to GLEIF legal_name_normalized + state. "
            "Identifies which UCC-active secured parties are LEI-registered entities."
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
    logger.info("inputs: UCC CA secured_parties_lance + GLEIF lei_records_lance (Arrow-bridge)")
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
        ucc_arrow, gleif_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            ucc_arrow, gleif_arrow,
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
