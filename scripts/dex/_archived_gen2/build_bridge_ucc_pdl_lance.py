#!/usr/bin/env python3
"""DuckDB bridge generator: UCC secured parties × PDL companies, two-path.

Cycle: ucc-gleif-identity-spine (s4).

Two-path bridge (audit decision B — UNION ALL with explicit match_path):

  Path A (via_gleif_parent): UCC secured party → ucc_gleif_lance (LEI) →
    lei_with_parent_lance (ultimate parent LEI) → PDL via ultimate_parent_name_normalized.
    For entities where the UCC entry resolved to a GLEIF LEI.

  Path B (direct): UCC secured party → PDL via direct name+state match.
    Only for entities that did NOT resolve to GLEIF (WHERE ug.lei IS NULL).

Output: polaris-warehouse/bridges/ucc_pdl_lance/

Arrow-bridge pattern (NOT the lance-duckdb extension).

CRITICAL: UCC secured_parties_lance has NO pre-normalized name column.
Inline normalization applied to ORG_NAME. Filter SECURED_PARTY_TYPE='Organization'.

Floor: ≥ 300,000 rows.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ucc_pdl_lance.py --apply
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
logger = logging.getLogger("build_bridge_ucc_pdl_lance")

BRIDGE_NAME = "ucc_pdl"
METHOD_NAME = "company_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "ucc_ca_secured_parties_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

UCC_SP_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
GLEIF_PARENT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_with_parent_lance"
UCC_GLEIF_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_gleif_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_pdl_lance"
DATASET_SLUG = "ucc_pdl_lance"

COLLISION_THRESHOLD = 50
# Floor recalibrated after entity-level dedup (fix-forward #5).
# Dry-run on 95K distinct UCC entities × 8.8M PDL via GLEIF-parent path
# yields ~18K clean matches after collision-rejection (12.5K platinum
# 1:1 + ~5.5K gold/silver). The original 300K floor was set for the
# filing-level (4.7M-row) intermediate that caused the OOM; entity-level
# is the correct join shape and produces a tighter, higher-quality set.
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
    import pyarrow as pa
    import pyarrow.compute as pc
    from scripts._lib.entity_name_normalize import normalize_entity_name as py_normalize

    logger.info("opening ucc_ca/secured_parties_lance ...")
    ucc_ds = lance.dataset(UCC_SP_LANCE_URI, storage_options=storage_options)
    ucc_raw = ucc_ds.scanner(
        columns=["SECURED_PARTY_TYPE", "ORG_NAME", "STATE"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
    ).to_table()
    rows_ucc = len(ucc_raw)
    logger.info("  ucc secured_parties (SECURED_PARTY_TYPE=Organization): %d rows", rows_ucc)

    # Pre-normalize in Python (avoids DuckDB regex spill of 4.7M rows)
    logger.info("  normalizing ORG_NAME in Python ...")
    org_names = ucc_raw.column("ORG_NAME").to_pylist()
    states = ucc_raw.column("STATE").to_pylist()
    norm_names = [py_normalize(n) if n else None for n in org_names]
    norm_states = [s.strip().upper() if s and len(s.strip()) == 2 else None for s in states]

    # Build pre-normalized ucc_branded arrow table (only non-null).
    # DEDUP at build time: UCC has heavy duplication (one secured-party
    # appears in many UCC1 filings — e.g., GoodLeap shows up 117K times,
    # JPMorgan 70K times). The bridge resolves ENTITIES not FILINGS, so
    # joining on the deduped distinct (name, state) set is the correct
    # semantics AND avoids cartesian explosion in the downstream
    # hash-joins that caused the prior cycle's 111 GiB OOM spill.
    branded_set: set = set()
    for nm, st in zip(norm_names, norm_states):
        if nm and st:
            branded_set.add((nm, st))
    branded_rows = list(branded_set)
    logger.info(
        "  ucc_branded (distinct name+state, deduped from %d raw rows): %d rows",
        len(norm_names), len(branded_rows),
    )
    ucc_branded_arrow = pa.table({
        "secured_party_name_normalized": pa.array([r[0] for r in branded_rows], type=pa.string()),
        "secured_party_state": pa.array([r[1] for r in branded_rows], type=pa.string()),
    })
    del ucc_raw, org_names, states, norm_names, norm_states, branded_rows, branded_set
    rows_ucc = len(ucc_branded_arrow)

    logger.info("opening gleif/lei_with_parent_lance ...")
    lwp_ds = lance.dataset(GLEIF_PARENT_LANCE_URI, storage_options=storage_options)
    lwp_arrow = lwp_ds.scanner(
        columns=["lei", "ultimate_parent_lei", "ultimate_parent_name_normalized"]
    ).to_table()
    logger.info("  lei_with_parent_lance: %d rows", len(lwp_arrow))

    logger.info("opening bridges/ucc_gleif_lance ...")
    ug_ds = lance.dataset(UCC_GLEIF_LANCE_URI, storage_options=storage_options)
    ug_arrow = ug_ds.scanner(
        columns=["secured_party_name_normalized", "secured_party_state", "lei", "confidence_tier"]
    ).to_table()
    logger.info("  ucc_gleif_lance: %d rows", len(ug_arrow))

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id", "pdl_name", "legal_name_normalized", "state",
            "pdl_website", "pdl_linkedin_url", "pdl_industry",
            "pdl_size", "pdl_founded", "pdl_locality",
        ]
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return ucc_branded_arrow, lwp_arrow, ug_arrow, pdl_arrow, rows_ucc, rows_pdl


def _build_match_table(
    ucc_branded_arrow, lwp_arrow, ug_arrow, pdl_arrow,
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
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    # ucc_branded_arrow is already pre-normalized in Python — register directly
    con.register("ucc_branded", ucc_branded_arrow)
    con.register("lei_with_parent", lwp_arrow)
    con.register("ucc_gleif", ug_arrow)
    con.register("pdl_raw", pdl_arrow)
    rows_ucc = con.execute("SELECT COUNT(*) FROM ucc_branded").fetchone()[0]
    logger.info("  ucc_branded (pre-normalized, from Python): %d", rows_ucc)

    # Path A: via GLEIF parent — computed in steps to avoid materializing huge intermediates
    # Step A1: UCC entries that have a GLEIF LEI match, enriched with ultimate parent name
    con.execute("""
        CREATE TEMP TABLE ucc_with_parent AS
        SELECT
            u.secured_party_name_normalized,
            u.secured_party_state,
            ug.lei,
            lwp.ultimate_parent_lei,
            lwp.ultimate_parent_name_normalized
        FROM ucc_branded u
        JOIN ucc_gleif ug
          ON ug.secured_party_name_normalized = u.secured_party_name_normalized
        JOIN lei_with_parent lwp
          ON lwp.lei = ug.lei
        WHERE lwp.ultimate_parent_name_normalized IS NOT NULL
    """)
    rows_uwp = con.execute("SELECT COUNT(*) FROM ucc_with_parent").fetchone()[0]
    logger.info("  ucc_with_parent (UCC→GLEIF→parent): %d rows", rows_uwp)

    # Step A2: Distinct parent names to pre-filter PDL (avoid full 8.8M scan per join)
    con.execute("""
        CREATE TEMP TABLE parent_names AS
        SELECT DISTINCT ultimate_parent_name_normalized AS norm_name
        FROM ucc_with_parent
    """)

    # Step A3: PDL slice for parent-name matches only
    con.execute("""
        CREATE TEMP TABLE pdl_parent_slice AS
        SELECT p.*
        FROM (SELECT * FROM pdl_raw WHERE legal_name_normalized IS NOT NULL AND state IS NOT NULL) AS p
        JOIN parent_names pn ON pn.norm_name = p.legal_name_normalized
    """)

    con.execute("""
        CREATE TEMP TABLE path_a AS
        SELECT
            u.secured_party_name_normalized,
            u.secured_party_state,
            u.ultimate_parent_lei,
            u.ultimate_parent_name_normalized,
            p.pdl_id,
            p.pdl_name,
            p.pdl_website,
            p.pdl_linkedin_url,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_founded,
            p.pdl_locality,
            'via_gleif_parent'              AS match_path
        FROM ucc_with_parent u
        JOIN pdl_parent_slice p
          ON p.legal_name_normalized = u.ultimate_parent_name_normalized
    """)
    rows_a = con.execute("SELECT COUNT(*) FROM path_a").fetchone()[0]
    logger.info("  path_a (via_gleif_parent): %d rows", rows_a)

    # Path B: direct match — entities WITHOUT a GLEIF resolution.
    # OOM-resistant rewrite (fix-forward #5 after the prior cycle deferred
    # this surface to operator follow-up): drop the pdl_direct_slice
    # intermediate which was materializing pdl_raw × ucc_no_gleif on JUST
    # legal_name_normalized — non-selective key, cartesian explosion on
    # common UCC names (smith, abc, etc.), 111 GiB temp spill on 4 local
    # runs. Replace with a single (name, state) composite-key INNER JOIN
    # gated by NOT EXISTS against ucc_gleif. ucc_branded is already
    # deduped to distinct (name, state) at Python-build time (above), so
    # the hash side is ~100-200K rows not 4.7M.
    con.execute("""
        CREATE TEMP TABLE ucc_branded_no_gleif AS
        SELECT u.secured_party_name_normalized, u.secured_party_state
        FROM ucc_branded u
        WHERE NOT EXISTS (
            SELECT 1 FROM ucc_gleif g
            WHERE g.secured_party_name_normalized = u.secured_party_name_normalized
        )
    """)
    rows_no_gleif = con.execute("SELECT COUNT(*) FROM ucc_branded_no_gleif").fetchone()[0]
    logger.info("  ucc_branded_no_gleif (no GLEIF match): %d rows", rows_no_gleif)

    # Composite-key (name, state) join — selective, streams without spilling.
    # pdl_raw filtered inline (no separate slice materialization).
    con.execute("""
        CREATE TEMP TABLE path_b AS
        SELECT
            u.secured_party_name_normalized,
            u.secured_party_state,
            NULL::TEXT                      AS ultimate_parent_lei,
            NULL::TEXT                      AS ultimate_parent_name_normalized,
            p.pdl_id,
            p.pdl_name,
            p.pdl_website,
            p.pdl_linkedin_url,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_founded,
            p.pdl_locality,
            'direct'                        AS match_path
        FROM ucc_branded_no_gleif u
        JOIN pdl_raw p
          ON p.legal_name_normalized = u.secured_party_name_normalized
         AND p.state                 = u.secured_party_state
        WHERE p.legal_name_normalized IS NOT NULL
          AND p.state IS NOT NULL
    """)
    rows_b = con.execute("SELECT COUNT(*) FROM path_b").fetchone()[0]
    logger.info("  path_b (direct): %d rows", rows_b)

    # Combine and apply fan-out tiering on the combined set
    con.execute("""
        CREATE TEMP TABLE combined AS
        SELECT * FROM path_a
        UNION ALL
        SELECT * FROM path_b
    """)

    con.execute("""
        CREATE TEMP TABLE ucc_fanout AS
        SELECT secured_party_name_normalized AS norm_name, secured_party_state AS state,
               COUNT(*) AS ucc_fan_out
        FROM combined
        GROUP BY secured_party_name_normalized, secured_party_state
    """)
    con.execute("""
        CREATE TEMP TABLE pdl_fanout AS
        SELECT pdl_id, COUNT(*) AS pdl_fan_out
        FROM combined
        GROUP BY pdl_id
    """)

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            c.secured_party_name_normalized,
            c.secured_party_state,
            c.match_path,
            c.pdl_id,
            c.ultimate_parent_lei,
            c.ultimate_parent_name_normalized,
            c.pdl_name,
            c.pdl_website,
            c.pdl_linkedin_url,
            c.pdl_industry,
            c.pdl_size,
            c.pdl_founded,
            c.pdl_locality,
            uf.ucc_fan_out,
            pf.pdl_fan_out,
            CASE
                WHEN uf.ucc_fan_out > {COLLISION_THRESHOLD}
                  OR pf.pdl_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.ucc_fan_out = 1 AND pf.pdl_fan_out = 1
                    THEN 'platinum'
                WHEN uf.ucc_fan_out = 1 OR pf.pdl_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                             AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM combined c
        JOIN ucc_fanout uf
          ON uf.norm_name = c.secured_party_name_normalized
         AND uf.state = c.secured_party_state
        JOIN pdl_fanout pf
          ON pf.pdl_id = c.pdl_id
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
        "rows_path_a": rows_a,
        "rows_path_b": rows_b,
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
        input_columns_right=["legal_name_normalized", "state"],
        output_value_description="normalized name + 2-letter state join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "UCC CA secured parties × PDL companies (GLEIF-parent-aware). "
            "Two-path: via_gleif_parent (UCC→GLEIF LEI→parent→PDL) and "
            "direct (name+state). match_path column discriminates paths."
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
    logger.info("inputs: UCC secured_parties + lei_with_parent + ucc_gleif + PDL (Arrow-bridge)")
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
        ucc_branded_arrow, lwp_arrow, ug_arrow, pdl_arrow, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            ucc_branded_arrow, lwp_arrow, ug_arrow, pdl_arrow,
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
        logger.info("  path_a (via_gleif_parent): %d", counts["rows_path_a"])
        logger.info("  path_b (direct):           %d", counts["rows_path_b"])

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
                "rows_path_a": counts["rows_path_a"],
                "rows_path_b": counts["rows_path_b"],
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
