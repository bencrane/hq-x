#!/usr/bin/env python3
"""DuckDB bridge generator: UCC debtors × SBA borrowers (Lance edition).

**v2.0.0 (address-axis composite, state-agnostic matcher):** matches on
(name, state) AND checks whether the UCC debtor's pre-baked normalized
address agrees with the SBA borrower's `borrstreet_normalized`. Address
agreement promotes the name-axis fan-out tier; absence holds/demotes:

    platinum  ← name 1:1 AND address_agrees
    gold      ← (name 1:1 AND no address) OR (name 1:N|N:1 AND address_agrees)
    silver    ← residual (no address corroboration OR name N:M)
    rejected  ← either side's fan-out > 50 (unchanged)

The legacy v1.0.0 fan-out-only tier is preserved in `name_confidence_tier`.
Method: `legal_name_state_exact_with_address_corroboration` v1.0.0 —
state-AGNOSTIC matcher (no CA hardcode) so the same method serves future
CO/TX/NY UCC × SBA rebuilds without redefinition.

Cycle: ucc-gleif-identity-spine (s6).

Reads:
  UCC: polaris-warehouse/ucc_ca/debtors_lance/    (5.86M rows; `address_base_normalized` baked PR #803)
  SBA: polaris-warehouse/sba/borrowers_lance/      (12M rows; `borrstreet_normalized` baked PR #782)

Arrow-bridge pattern (NOT the lance-duckdb extension).

CRITICAL: UCC debtors_lance has NO pre-normalized name column. Inline
normalization applied to ORG_NAME via _normalize_entity_sql(). Filter
DEBTOR_TYPE='Organization' to organizations only.

Join: (ucc_debtor_name_normalized, state, address) × (legal_name_normalized, borrstate, borrstreet_normalized).
The (name, state) join is exact; the address is a non-fatal tier promoter
(absent or differing address keeps the row in the match but caps its tier).

Powers the outcome-detection mechanic: when a Capital Expansion match closes
→ new UCC-1 fires → Stripe origination % billing trigger.

Output: polaris-warehouse/bridges/ucc_sba_borrower_lance/
Floor: ≥ 100,000 rows.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ucc_sba_borrower_lance.py --apply
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
logger = logging.getLogger("build_bridge_ucc_sba_borrower_lance")

BRIDGE_NAME = "ucc_sba_borrower"
METHOD_NAME = "legal_name_state_exact_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

SOURCE_LEFT = "ucc_ca_debtors_lance"
SOURCE_RIGHT = "sba_borrowers_lance"

UCC_DEBTORS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_borrower_lance"
DATASET_SLUG = "ucc_sba_borrower_lance"

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

    logger.info("opening ucc_ca/debtors_lance via Arrow-bridge ...")
    ucc_ds = lance.dataset(UCC_DEBTORS_LANCE_URI, storage_options=storage_options)
    ucc_arrow = ucc_ds.scanner(
        columns=["UCC1_NUM", "DEBTOR_TYPE", "ORG_NAME", "STATE", "address_base_normalized"],
        filter=pc.field("DEBTOR_TYPE") == "Organization",
    ).to_table()
    rows_left = len(ucc_arrow)
    logger.info("  ucc_ca/debtors_lance (DEBTOR_TYPE=Organization): %d rows", rows_left)

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
            -- address agreement: UCC debtor's normalized address equals SBA borrower's normalized street
            (
              u.ucc_address_base_normalized IS NOT NULL
              AND s.borrstreet_normalized IS NOT NULL
              AND u.ucc_address_base_normalized = s.borrstreet_normalized
            )                                    AS address_agrees,
            -- which path matched (single-axis for this bridge — either it matches or it doesn't)
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
            -- legacy name-only tier (v1.0.0 semantic, preserved for backward-compat consumers)
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

    # Composite tier — address agreement promotes; absence holds/demotes.
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

    # Per-state rollup for the multi-state-aware composite (Option B per operator call —
    # the matcher is state-agnostic; this log lets the operator inspect per-state coverage).
    state_breakdown = con.execute("""
        SELECT state,
               COUNT(*) AS n,
               COUNT(*) FILTER (WHERE confidence_tier='platinum') AS plat,
               COUNT(*) FILTER (WHERE address_agrees)             AS addr_agrees
        FROM bridge_match
        GROUP BY state
        ORDER BY n DESC
        LIMIT 15
    """).fetchall()
    logger.info("  per-state rollup (top 15 by row count):")
    for row in state_breakdown:
        st, n, plat, addr = row
        logger.info("    %-3s rows=%-10d platinum=%-8d address_agrees=%d", st, n, plat, addr)

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
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "State-AGNOSTIC matcher (no CA hardcode): inner-joins on exact-equality "
            "normalized legal name AND 2-letter US state; then checks whether LEFT's "
            "normalized physical address (base form, unit-stripped via "
            "_lib/address_normalize) equals RIGHT's normalized street address. "
            "Address agreement promotes the name-axis fan-out tier: platinum "
            "requires BOTH 1:1 name AND address corroboration. Same composite "
            "method as the CA-scoped UCC×SoS and UCC×SAM bridges; reusable for "
            "future CO/TX/NY UCC × SBA rebuilds without changing the method "
            "definition."
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
            "address_agrees; without address corroboration the name-axis platinum "
            "demotes to gold and gold demotes to silver."
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
            "UCC CA debtors (org-type, inline-normalized) × SBA 7a/504 borrowers — "
            "v2.0.0 state-agnostic composite name+address axis. Multi-state on the "
            "UCC debtor side (debtor's mailing state, not the UCC filing state); "
            "CA-heavy at ~98% of rows by construction (UCC source is CA filings) "
            "but accepts cross-state debtor matches. Address agreement promotes the "
            "fan-out tier. Powers outcome-detection: SBA borrower with a UCC-1 "
            "filing matching on both name+state AND address = closed-loop "
            "capital-deployment signal."
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
    logger.info("inputs: UCC debtors_lance + SBA borrowers_lance (Arrow-bridge)")
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
