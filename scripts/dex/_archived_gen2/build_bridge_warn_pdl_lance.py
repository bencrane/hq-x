#!/usr/bin/env python3
"""DuckDB bridge generator: WARN Act notices x PDL companies (company name).

Cycle: warn-pdl-identity-bridge (2026-05-20).

Single-path Pattern B bridge. Foundational identity layer for the WARN ->
private-credit GTM: links each WARN Act layoff notice to a PDL company
(`pdl_id`). Once a WARN notice carries a pdl_id it joins straight into the
already-built `bridges/sam_pdl_usaspending_lance` cohort (by pdl_id) for
authoritative SAM NAICS (company type) + USAspending federal-contract health
signals -- so this one bridge unlocks the whole enrichment spine.

MATCH KEY: normalized company name ONLY (not name+state). Rationale (dry-run
diagnostic, 2026-05-20): WARN `postal_code` is the state of the *layoff
facility*, NOT the company's HQ/identity state. PDL `state` is the company HQ
state. For multi-state employers -- which file the most WARN notices (American
Airlines, AT&T, Johnson Controls, Weyerhaeuser, ...) -- the two systematically
disagree. Name+state matched only 15.4% of distinct WARN companies; name-only
matched 37.5%, and the 11,397-entity gap is dominated by exactly these
legitimate multi-state-employer matches. Name+state was matching on the wrong
field, not matching more strictly. 76% of name-only matches resolve to a
globally-unique PDL company. State is preserved as the `state_agreement`
confidence column (not dropped), so downstream consumers keep the signal.

Method `company_name_exact` v1.0.0 is REUSED per L21 -- only register_bridge is
called, never register_match_method*.

Both sides come PRE-NORMALIZED -- no normalization pass in this script:
  - WARN side: warn/notices_lance.company_normalized      (entity_name_normalize v1.0.0)
  - PDL  side: pdl/free_companies_lance.legal_name_normalized  (same rule)

Grain: fan-out / confidence tiering is computed at DISTINCT-company-name grain
so a company with N WARN notices is not mis-tiered. The OUTPUT is notice grain
-- one row per (WARN notice, matched pdl_id) -- because each WARN notice is a
distinct trigger event. confidence_tier: platinum = name globally unique in
PDL; gold = name shared by 2-50 PDL companies; rejected = >50 (consumer should
disambiguate gold rows via state_agreement / downstream signals).

Output: polaris-warehouse/bridges/warn_pdl_lance/  (BTREE on warn_hash_id, pdl_id)

Arrow-bridge pattern (NOT the lance-duckdb extension).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_warn_pdl_lance.py --dry-run
  # then --apply
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
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_warn_pdl_lance")

BRIDGE_NAME = "warn_pdl"
METHOD_NAME = "company_name_exact"   # REUSED — pre-registered method
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "warn_notices_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

WARN_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/warn/notices_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/warn_pdl_lance"
DATASET_SLUG = "warn_pdl_lance"

COLLISION_THRESHOLD = 50
# Floor calibrated from the dry-run yield (~half observed, per UCC×PDL precedent).
MIN_ROWS_MATCHED = 30_000
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
    """Open both Lance datasets, project the needed columns to Arrow tables."""
    import lance

    logger.info("opening warn/notices_lance ...")
    warn_ds = lance.dataset(WARN_LANCE_URI, storage_options=storage_options)
    warn_arrow = warn_ds.scanner(
        columns=["hash_id", "company", "company_normalized", "postal_code"],
    ).to_table()
    rows_warn = len(warn_arrow)
    logger.info("  warn notices: %d rows", rows_warn)

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id", "pdl_name", "legal_name_normalized", "state",
            "pdl_website", "pdl_linkedin_url", "pdl_industry",
            "pdl_size", "pdl_founded", "pdl_locality",
        ],
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies: %d rows", rows_pdl)

    return warn_arrow, pdl_arrow, rows_warn, rows_pdl


def _build_match_table(
    warn_arrow, pdl_arrow, *, bridge_run_id: str, generated_at_iso: str,
) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("warn_raw", warn_arrow)
    con.register("pdl_raw", pdl_arrow)

    # WARN notices with a usable name key (notice grain — keeps hash_id).
    # postal_code is carried as warn_state but is NOT a match key (it is the
    # layoff-facility state, not the company identity state).
    con.execute("""
        CREATE TEMP TABLE warn_keyed AS
        SELECT hash_id, company, company_normalized, postal_code AS warn_state
        FROM warn_raw
        WHERE company_normalized IS NOT NULL
          AND trim(company_normalized) <> ''
    """)
    rows_warn_keyed = con.execute("SELECT COUNT(*) FROM warn_keyed").fetchone()[0]
    logger.info("  warn_keyed (notice grain, has name): %d rows", rows_warn_keyed)

    # PDL companies with a usable name. state is carried (pdl_state) but is not
    # a match key.
    con.execute("""
        CREATE TEMP TABLE pdl_branded AS
        SELECT pdl_id, pdl_name, legal_name_normalized, state AS pdl_state,
               pdl_website, pdl_linkedin_url, pdl_industry,
               pdl_size, pdl_founded, pdl_locality
        FROM pdl_raw
        WHERE legal_name_normalized IS NOT NULL
          AND trim(legal_name_normalized) <> ''
    """)
    rows_pdl_branded = con.execute("SELECT COUNT(*) FROM pdl_branded").fetchone()[0]
    logger.info("  pdl_branded (has name): %d rows", rows_pdl_branded)

    # Distinct WARN company names — fan-out / tiering is computed at this grain
    # (not notice grain), so a company with N WARN notices is not mis-tiered.
    con.execute("""
        CREATE TEMP TABLE warn_keys AS
        SELECT DISTINCT company_normalized FROM warn_keyed
    """)
    rows_warn_keys = con.execute("SELECT COUNT(*) FROM warn_keys").fetchone()[0]
    logger.info("  warn_keys (distinct company names): %d rows", rows_warn_keys)

    # Distinct-name x PDL exact-equality match on normalized company name.
    con.execute("""
        CREATE TEMP TABLE key_match AS
        SELECT
            w.company_normalized,
            p.pdl_id,
            p.pdl_name,
            p.pdl_state,
            p.pdl_website,
            p.pdl_linkedin_url,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_founded,
            p.pdl_locality
        FROM warn_keys w
        JOIN pdl_branded p
          ON p.legal_name_normalized = w.company_normalized
    """)
    rows_key_match = con.execute("SELECT COUNT(*) FROM key_match").fetchone()[0]
    logger.info("  key_match (distinct-name x pdl): %d rows", rows_key_match)

    # Fan-out at distinct-name grain.
    con.execute("""
        CREATE TEMP TABLE warn_fanout AS
        SELECT company_normalized, COUNT(*) AS warn_fan_out
        FROM key_match GROUP BY company_normalized
    """)
    con.execute("""
        CREATE TEMP TABLE pdl_fanout AS
        SELECT pdl_id, COUNT(*) AS pdl_fan_out
        FROM key_match GROUP BY pdl_id
    """)

    # Tier each name match, then explode to notice grain by joining back to
    # warn_keyed — one output row per (WARN notice, matched pdl_id).
    # state_agreement: does the WARN layoff state equal this PDL company's HQ
    # state — a confidence signal, NOT a filter.
    # pdl_industry is carried but is LOW-TRUST (self-selected LinkedIn data) —
    # downstream company-type filtering must use SAM NAICS via
    # sam_pdl_usaspending_lance, not pdl_industry.
    con.execute(f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            wk.hash_id                              AS warn_hash_id,
            wk.company                              AS warn_company,
            km.company_normalized                   AS warn_company_normalized,
            wk.warn_state,
            km.pdl_id,
            km.pdl_name,
            km.pdl_state,
            km.pdl_website,
            km.pdl_linkedin_url,
            km.pdl_industry,
            km.pdl_size,
            km.pdl_founded,
            km.pdl_locality,
            (wk.warn_state IS NOT NULL AND km.pdl_state IS NOT NULL
               AND upper(trim(wk.warn_state)) = upper(trim(km.pdl_state)))
                                                    AS state_agreement,
            '{METHOD_NAME}'                         AS match_method,
            km.company_normalized                   AS match_value,
            wf.warn_fan_out,
            pf.pdl_fan_out,
            CASE
                WHEN wf.warn_fan_out > {COLLISION_THRESHOLD}
                  OR pf.pdl_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN wf.warn_fan_out = 1 AND pf.pdl_fan_out = 1
                    THEN 'platinum'
                WHEN wf.warn_fan_out = 1 OR pf.pdl_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                     AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'           AS generated_at,
            '{BRIDGE_VERSION}'                       AS bridge_version,
            '{bridge_run_id}'                        AS bridge_run_id
        FROM key_match km
        JOIN warn_keyed wk
          ON wk.company_normalized = km.company_normalized
        JOIN warn_fanout wf
          ON wf.company_normalized = km.company_normalized
        JOIN pdl_fanout pf
          ON pf.pdl_id = km.pdl_id
    """)
    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver'),
               COUNT(*) FILTER (WHERE state_agreement)
        FROM bridge_match
    """).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]
    distinct_warn = con.execute(
        "SELECT COUNT(DISTINCT warn_hash_id) FROM bridge_match"
    ).fetchone()[0]
    distinct_pdl = con.execute(
        "SELECT COUNT(DISTINCT pdl_id) FROM bridge_match"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_state_agree": row_counts[4],
        "rows_collision_rejected": rejected,
        "distinct_warn_notices_matched": distinct_warn,
        "distinct_pdl_companies_matched": distinct_pdl,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

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
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, time.time() - t0, ds.version
        )

        for col in ("warn_hash_id", "pdl_id"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s OK", col)
            except Exception as e:
                logger.warning("  BTREE on %s failed (non-fatal): %s", col, e)
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
    """Register the bridge instance only.

    Method `company_name_exact` v1.0.0 already exists. Per L21 +
    DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern B" key callout, a bridge
    reusing an existing method MUST NOT call register_match_method /
    register_match_method_version — the helper does ON CONFLICT DO UPDATE and
    would overwrite the original method's config.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "WARN Act layoff notices x PDL companies via normalized "
            "company-name exact match. Foundational identity layer for the "
            "WARN -> private-credit GTM: links each WARN notice to a PDL "
            "company (pdl_id), which joins to sam_pdl_usaspending_lance for "
            "NAICS + federal-contract health signals. state_agreement column "
            "flags WARN-layoff-state vs PDL-HQ-state concordance."
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
    logger.info("normalizer (both sides pre-normalized): entity_name_normalize v%s", NORMALIZER_VERSION)
    logger.info("inputs: warn/notices_lance + pdl/free_companies_lance (Arrow-bridge)")
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
        warn_arrow, pdl_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            warn_arrow, pdl_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched (notice grain): %d", counts["rows_matched"])
        logger.info("    platinum (unique name):    %d", counts["rows_tier1"])
        logger.info("    gold     (name shared 2-50): %d", counts["rows_tier2"])
        logger.info("    silver:                    %d", counts["rows_tier3"])
        logger.info("  rows_collision_rejected (>50): %d", counts["rows_collision_rejected"])
        logger.info("  rows with state_agreement:   %d", counts["rows_state_agree"])
        logger.info("  distinct WARN notices matched: %d", counts["distinct_warn_notices_matched"])
        logger.info("  distinct PDL companies matched: %d", counts["distinct_pdl_companies_matched"])

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
            },
        )
        logger.info("OK — run_id=%s  lance_rows=%d  duration=%.1fs", bridge_run_id, lance_count, time.time() - t0)
        logger.info("    output: %s", BRIDGE_LANCE_URI)
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
