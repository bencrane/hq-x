#!/usr/bin/env python3
"""DuckDB bridge generator: UCC CO debtors × SBA borrowers (Lance edition).

CO parity port of build_bridge_ucc_sba_borrower_lance.py (the CA edition).

Reads:
  UCC: polaris-warehouse/ucc_co/debtors_lance/     (1.99M rows, raw — no pre-normalized name)
  SBA: polaris-warehouse/sba/borrowers_lance/       (12M rows)

Arrow-bridge pattern (NOT the lance-duckdb extension).

CRITICAL: UCC debtors_lance has NO pre-normalized name column. Inline
normalization applied to ORG_NAME via _normalize_entity_sql(). Filter
DEBTOR_TYPE='Organization' to organizations only.

Join: (ucc_debtor_name_normalized, state) × (legal_name_normalized, borrstate).

Method company_name_state_exact v1.0.0 is REUSED (registered by the CA ucc_gleif
bridge). Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md, a bridge reusing an existing
method calls only register_bridge().

Output: polaris-warehouse/bridges/ucc_co_sba_borrower_lance/
Floor: MIN_ROWS_MATCHED — calibrated from --dry-run.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ucc_co_sba_borrower_lance.py --apply
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
logger = logging.getLogger("build_bridge_ucc_co_sba_borrower_lance")

BRIDGE_NAME = "ucc_co_sba_borrower"
METHOD_NAME = "legal_name_state_exact_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

SOURCE_LEFT = "ucc_co_debtors_lance"
SOURCE_RIGHT = "sba_borrowers_lance"

UCC_DEBTORS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance"
SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_co_sba_borrower_lance"
DATASET_SLUG = "ucc_co_sba_borrower_lance"

COLLISION_THRESHOLD = 50
# Calibrated from --dry-run: natural matched-row count 357,772 (19,662
# platinum + 146,889 gold + 191,221 silver). Floor set conservatively
# below it as a regression tripwire.
MIN_ROWS_MATCHED = 200_000
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

    logger.info("opening ucc_co/debtors_lance via Arrow-bridge ...")
    ucc_ds = lance.dataset(UCC_DEBTORS_LANCE_URI, storage_options=storage_options)
    ucc_arrow = ucc_ds.scanner(
        columns=["UCC1_NUM", "DEBTOR_TYPE", "ORG_NAME", "STATE", "address_base_normalized"],
        filter=pc.field("DEBTOR_TYPE") == "Organization",
    ).to_table()
    rows_left = len(ucc_arrow)
    logger.info("  ucc_co/debtors_lance (DEBTOR_TYPE=Organization): %d rows", rows_left)

    logger.info("opening sba/borrowers_lance via Arrow-bridge ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_arrow = sba_ds.scanner(
        columns=[
            "legal_name_normalized", "borrstate", "borrzip",
            "total_loans", "total_gross_approval", "latest_loanstatus",
            "has_pending_commit", "naics_codes_set", "franchise_brands_set", "lender_set",
            "borrstreet_normalized",
        ]
    ).to_table()
    rows_right = len(sba_arrow)
    logger.info("  sba/borrowers_lance: %d rows", rows_right)

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

    norm_expr = _normalize_entity_sql("ORG_NAME")

    con.execute(
        f"""
        CREATE TEMP TABLE ucc_branded AS
        SELECT
            ({norm_expr})                   AS debtor_name_normalized,
            upper(trim(STATE))              AS ucc_state,
            address_base_normalized         AS ucc_address_base_normalized
        FROM ucc_raw
        WHERE ({norm_expr}) IS NOT NULL
          AND STATE IS NOT NULL
          AND length(trim(STATE)) = 2
        """
    )
    rows_ucc = con.execute("SELECT COUNT(*) FROM ucc_branded").fetchone()[0]
    logger.info("  ucc_branded (name+state non-null): %d", rows_ucc)

    con.execute("""
        CREATE TEMP TABLE sba_clean AS
        SELECT * FROM sba_raw
        WHERE legal_name_normalized IS NOT NULL AND borrstate IS NOT NULL
    """)

    logger.info("computing fan-out tables ...")
    con.execute("""
        CREATE TEMP TABLE ucc_fanout AS
        SELECT debtor_name_normalized AS norm_name, ucc_state AS state,
               COUNT(*) AS ucc_fan_out
        FROM ucc_branded GROUP BY debtor_name_normalized, ucc_state
    """)
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT legal_name_normalized AS norm_name, borrstate AS state,
               COUNT(*) AS sba_fan_out
        FROM sba_clean GROUP BY legal_name_normalized, borrstate
    """)

    logger.info("computing tiered JOIN (v2.0.0 — address-axis composite) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            u.debtor_name_normalized,
            u.ucc_state                         AS state,
            u.ucc_address_base_normalized,
            s.legal_name_normalized,
            s.borrstate,
            s.borrzip,
            s.borrstreet_normalized             AS sba_borrstreet_normalized,
            s.total_loans,
            s.total_gross_approval,
            s.latest_loanstatus,
            s.has_pending_commit,
            s.naics_codes_set,
            s.franchise_brands_set,
            s.lender_set,
            uf.ucc_fan_out,
            sf.sba_fan_out,
            (
              u.ucc_address_base_normalized IS NOT NULL
              AND s.borrstreet_normalized IS NOT NULL
              AND u.ucc_address_base_normalized = s.borrstreet_normalized
            )                                    AS address_agrees,
            CASE
              WHEN u.ucc_address_base_normalized IS NOT NULL
                   AND s.borrstreet_normalized IS NOT NULL
                   AND u.ucc_address_base_normalized = s.borrstreet_normalized
              THEN 'street'
              ELSE NULL
            END                                  AS address_match_path,
            CASE
              WHEN u.ucc_address_base_normalized IS NOT NULL
                   AND s.borrstreet_normalized IS NOT NULL
                   AND u.ucc_address_base_normalized = s.borrstreet_normalized
              THEN u.ucc_address_base_normalized
              ELSE NULL
            END                                  AS address_match_value,
            CASE
                WHEN uf.ucc_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sba_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.ucc_fan_out = 1 AND sf.sba_fan_out = 1
                    THEN 'platinum'
                WHEN uf.ucc_fan_out = 1 OR sf.sba_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                  AS name_confidence_tier,
            TIMESTAMP '{generated_at_iso}'       AS generated_at,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id
        FROM ucc_branded u
        JOIN sba_clean s
          ON s.legal_name_normalized = u.debtor_name_normalized
         AND upper(s.borrstate) = u.ucc_state
        JOIN ucc_fanout uf
          ON uf.norm_name = u.debtor_name_normalized AND uf.state = u.ucc_state
        JOIN sba_fanout sf
          ON sf.norm_name = s.legal_name_normalized AND sf.state = s.borrstate
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_tiered AS
        SELECT
            b.*,
            CASE
                WHEN b.name_confidence_tier = 'rejected'                                  THEN 'rejected'
                WHEN b.name_confidence_tier = 'platinum' AND b.address_agrees             THEN 'platinum'
                WHEN b.name_confidence_tier = 'platinum' AND NOT b.address_agrees         THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND b.address_agrees             THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND NOT b.address_agrees         THEN 'silver'
                ELSE 'silver'
            END AS composite_confidence_tier
        FROM bridge_all b
        """
    )

    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            *,
            composite_confidence_tier AS confidence_tier
        FROM bridge_tiered
        WHERE composite_confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver'),
               COUNT(*) FILTER (WHERE address_agrees)
        FROM bridge_match
    """).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_tiered WHERE composite_confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_address_agrees": row_counts[4],
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
        for col in ("debtor_name_normalized", "state", "composite_confidence_tier", "address_agrees"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s failed (non-fatal): %s", col, e)
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
    # v2.0.0 — state-agnostic composite method shared with the sibling
    # ucc_sba_borrower_lance (CA-heavy multi-state). Idempotent UPSERT keeps
    # the row consistent across writers; safe even if the CA bridge hasn't
    # run yet.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "State-AGNOSTIC matcher (no CA/CO hardcode): inner-joins on "
            "exact-equality normalized legal name AND 2-letter US state; then "
            "checks whether LEFT's normalized physical address (base form, "
            "unit-stripped via _lib/address_normalize) equals RIGHT's normalized "
            "street address. Address agreement promotes the name-axis fan-out "
            "tier: platinum requires BOTH 1:1 name AND address corroboration."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py + _lib/address_normalize.py",
        normalizer_version=f"name v{NORMALIZER_VERSION} / addr v1.0.0",
        blacklist_module="(same as component normalizers)",
        blacklist_version=f"name v{NORMALIZER_VERSION} / addr v1.0.0",
        tier_rule_description=(
            "Name fan-out tier (1:1=platinum, 1:N|N:1=gold, N:M<=50=silver, "
            ">50=rejected) preserved in `name_confidence_tier`. Composite tier "
            "(in canonical `confidence_tier`): platinum REQUIRES name 1:1 AND "
            "address_agrees; absence demotes the name-axis platinum to gold "
            "and gold to silver."
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["ORG_NAME", "STATE", "address_base_normalized"],
        input_columns_right=["legal_name_normalized", "borrstate", "borrstreet_normalized"],
        output_value_description=(
            "(debtor_name_normalized, state) match + boolean address_agrees + "
            "address_match_path/value + composite_confidence_tier."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "UCC CO debtors (org-type, inline-normalized) × SBA 7a/504 borrowers "
            "— v2.0.0 state-agnostic composite name+address axis. CO sibling of "
            "ucc_sba_borrower_lance (which is CA-heavy by source-spine "
            "construction); uses the same composite method. Address agreement "
            "promotes the fan-out tier. Powers outcome-detection: SBA borrower "
            "with a UCC-1 filing matching on both name+state AND address = "
            "closed-loop capital-deployment signal."
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
    logger.info("inputs: UCC CO debtors_lance + SBA borrowers_lance (Arrow-bridge)")
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
        logger.info("bridge composite tier distribution (v2.0.0 — address-axis):")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum:               %d  (name 1:1 + address agrees)", counts["rows_tier1"])
        logger.info("    gold:                   %d  (name 1:1 OR (name 1:N|N:1 + address agrees))", counts["rows_tier2"])
        logger.info("    silver:                 %d  (residual; no address corroboration on non-1:1 name)", counts["rows_tier3"])
        logger.info("  address_agrees:           %d  (UCC address == SBA borrstreet)", counts["rows_address_agrees"])
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
                "rows_address_agrees": counts["rows_address_agrees"],
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
