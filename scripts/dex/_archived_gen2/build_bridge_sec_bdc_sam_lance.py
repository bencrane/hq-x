#!/usr/bin/env python3
"""DuckDB bridge generator: SEC BDC Schedule-of-Investments portfolio companies
x SAM.gov registered entities (Pattern B, name-keyed).

Cycle: sec-bdc-sam-bridge (2026-05-21).

Resolves BDC portfolio company names to SAM.gov UEIs via canonically-normalized
legal-business-name exact-equality match.  Both sides folded through the
canonical `normalize_entity_name` v1.0.0 from `_lib/entity_name_normalize.py`.

Left side  (BDC):
  - `sec_bdc/soi_lance` — rows where `portfolio_company_entity_type = 'company'`
  - Match keys: `portfolio_company_name_clean` (pipe-split on '|') PLUS
    `portfolio_company_dba` (secondary key).
  - Sector-header / structural-noise rows excluded post-normalization via the
    LEFT_SIDE_BLACKLIST (see below).

Right side (SAM.gov):
  - `sam_gov/entities_lance` — FULL ~884K registered entities (no award filter).
  - Match key: canonical `normalize_entity_name(legal_business_name)` — re-
    normalized from RAW.  Do NOT trust the pre-materialized
    `legal_business_name_normalized` column; it has only 90.8% canonical parity
    (built by an older normalizer revision).  Constraint enforced per directive.

Match method: `bdc_company_name_exact` (new, distinct from name+state methods).
Grain: DISTINCT (BDC company × SAM UEI) — one row per matched (normalized BDC
company name, SAM unique_entity_id) pair, NOT one per SOI filing occurrence.
Tier rule: platinum=1:1, gold=1:N, rejected=>50 (silver cannot occur — left
fan-out is 1 by construction; norm_name is the BDC company identity).
HARD-FAIL floor: 1,200 platinum+gold rows (~61% of the 1,963 measured
2026-05-21 at distinct grain — a defensible lower bound, not the target).

Output:
  s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sam_lance
  BTREE scalar index on `norm_name` (the join key).

Per-row provenance: `bridge_run_id`, `match_method`, `match_value`,
`confidence_tier`, per-side fan-out counts, `generated_at`.

Sector-header blacklist (LEFT_SIDE_BLACKLIST):
  Post-normalization strings that identify SOI sector-header rows mis-typed as
  `company` in the soi_lance emit (verified against directive ## Baseline):
    "software", "business services", "healthcare", "consumer services",
    "financial services", "company", "portfolio company",
    "non control non affiliate investments"
  Matched AFTER normalize_entity_name (so "Software" and "SOFTWARE" both hit).

SAM UEI column: `unique_entity_id` (there is no column named `uei` in
sam_gov/entities_lance — verified per Validator notes + directive Constraint).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" \\
    python apps/data-engine-x/scripts/build_bridge_sec_bdc_sam_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" \\
    python apps/data-engine-x/scripts/build_bridge_sec_bdc_sam_lance.py --dry-run
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
    normalize_entity_name,
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
logger = logging.getLogger("build_bridge_sec_bdc_sam_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sec_bdc_sam"
METHOD_NAME = "bdc_company_name_exact"   # NEW — name-only, no state; distinct
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sec_bdc_soi_lance"
SOURCE_RIGHT = "sam_gov_entities_lance"

# R2 layout ------------------------------------------------------------------
SOI_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance"
)
SAM_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sam_lance"
)
DATASET_SLUG = "sec_bdc_sam_lance"

# Tier threshold / floor -----------------------------------------------------
COLLISION_THRESHOLD = 50
MIN_ROWS_PLATINUM_GOLD = 1_200    # HARD-FAIL floor (~61% of 1,963 measured; distinct grain)
TMP_DIR = "/tmp/lance"

# Sector-header / structural-noise blacklist (post-normalization strings).
# These SOI rows are mis-typed as `company` but are really sector headers or
# structural boilerplate in the Schedule of Investments filings.  They match
# AFTER normalize_entity_name so the comparison is against the normalized form.
LEFT_SIDE_BLACKLIST: frozenset[str] = frozenset({
    "software",
    "business services",
    "healthcare",
    "consumer services",
    "financial services",
    "company",
    "portfolio company",
    "non control non affiliate investments",
})


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
    """Read SOI + SAM Lance datasets via PyLance scanner with column projection.

    SOI side: company-typed rows only; columns needed for left side + provenance.
    SAM side: FULL dataset (no award filter per directive Out-of-scope); only the
    two columns needed for the match (unique_entity_id + legal_business_name).
    """
    import lance
    import pyarrow.compute as pc

    logger.info("opening sec_bdc/soi_lance (company-typed rows) ...")
    soi_ds = lance.dataset(SOI_LANCE_URI, storage_options=storage_options)
    soi_total = soi_ds.count_rows()
    soi_arrow = soi_ds.scanner(
        columns=[
            "portfolio_company_name_clean",
            "portfolio_company_dba",
            "portfolio_company_entity_type",
            "adsh",
            "cik",
        ],
        filter=pc.field("portfolio_company_entity_type") == "company",
    ).to_table()
    rows_left_raw = len(soi_arrow)
    logger.info(
        "  soi_lance total=%d | company-typed rows=%d",
        soi_total, rows_left_raw,
    )

    logger.info("opening sam_gov/entities_lance (FULL, no award filter) ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_total = sam_ds.count_rows()
    sam_arrow = sam_ds.scanner(
        columns=["unique_entity_id", "legal_business_name"],
    ).to_table()
    rows_right_raw = len(sam_arrow)
    logger.info(
        "  sam_gov/entities_lance total=%d rows",
        rows_right_raw,
    )
    assert rows_right_raw == sam_total, (
        f"SAM scan mismatch: scanned={rows_right_raw} total={sam_total}"
    )

    return soi_arrow, sam_arrow, rows_left_raw, rows_right_raw


def _build_match_table(
    soi_arrow,
    sam_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize both sides, apply blacklist, fan-out, tier; return match table.

    Grain: DISTINCT (BDC company × SAM UEI). One output row per matched
    (normalized BDC company name, SAM unique_entity_id) pair — NOT one per SOI
    filing occurrence. A company appearing in many BDC filings contributes one
    bridge row per SAM UEI it resolves to, not one per filing.

    Left side normalization:
      1. Pipe-split `portfolio_company_name_clean` on '|' to explode multi-
         borrower cells (each segment is a separate key).
      2. Union in `portfolio_company_dba` as a second left key.
      3. Both sets of raw names folded through canonical `normalize_entity_name`.
      4. Sector-header blacklist applied post-normalization.
      5. Left side is DEDUPLICATED to one row per distinct norm_name — filing
         multiplicity is collapsed. norm_name IS the BDC company identity (the
         SOI side carries no other entity key), so left fan-out is 1 by
         construction.

    Right side normalization:
      - Raw `legal_business_name` re-normalized via the same function.
      - Do NOT use the pre-materialized `legal_business_name_normalized` column
        (only 90.8% canonical parity — directive Constraint).
      - Right deduplication: DISTINCT (unique_entity_id, norm_name) so each SAM
        entity appears once per norm key.

    Fan-out measured:
      - Left:  1 (norm_name is the BDC company identity — distinct grain).
      - Right: COUNT(DISTINCT unique_entity_id) per norm_name.
    Tier rule: platinum=1:1, gold=1:N (one name → 2..50 SAM UEIs),
    rejected=>50. silver (N:M) cannot occur — left fan-out is always 1.
    """
    import duckdb
    import pyarrow as pa

    logger.info("normalizing SOI left side (company_name_clean + dba) in Python ...")

    raw_clean = soi_arrow.column("portfolio_company_name_clean").to_pylist()
    raw_dba   = soi_arrow.column("portfolio_company_dba").to_pylist()

    # Build a flat list of (norm_key, key_source, raw_name) for left side.
    # Filing multiplicity is collapsed later in left_proj (DISTINCT norm_name);
    # the originating SOI filing row is not tracked — the bridge grain is the
    # distinct BDC company.
    left_rows = []  # (norm_key: str, key_source: str, raw_name: str)
    for clean, dba in zip(raw_clean, raw_dba):
        # Pipe-split the clean field; each segment is an independent left key.
        if clean is not None:
            for segment in str(clean).split("|"):
                segment = segment.strip()
                if segment:
                    norm = normalize_entity_name(segment)
                    if norm is not None and norm not in LEFT_SIDE_BLACKLIST:
                        left_rows.append((norm, "clean", segment))
        # DBA as a secondary key.
        if dba is not None:
            dba_s = str(dba).strip()
            if dba_s:
                norm = normalize_entity_name(dba_s)
                if norm is not None and norm not in LEFT_SIDE_BLACKLIST:
                    left_rows.append((norm, "dba", dba_s))

    logger.info(
        "  left_rows (exploded, pre-dedup, post-blacklist): %d",
        len(left_rows),
    )

    left_tbl = pa.table({
        "norm_name":    pa.array([r[0] for r in left_rows], type=pa.string()),
        "key_source":   pa.array([r[1] for r in left_rows], type=pa.string()),
        "bdc_raw_name": pa.array([r[2] for r in left_rows], type=pa.string()),
    })
    logger.info("  left_tbl rows=%d", len(left_tbl))

    logger.info("normalizing SAM right side (raw legal_business_name) in Python ...")
    sam_uei   = sam_arrow.column("unique_entity_id").to_pylist()
    sam_legal = sam_arrow.column("legal_business_name").to_pylist()
    sam_norm  = [normalize_entity_name(n) for n in sam_legal]

    right_tbl = pa.table({
        "unique_entity_id":   pa.array(sam_uei,   type=pa.string()),
        "legal_business_name": pa.array(sam_legal, type=pa.string()),
        "norm_name":          pa.array(sam_norm,  type=pa.string()),
    })
    logger.info("  right_tbl rows=%d", len(right_tbl))

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='80GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("left_raw", left_tbl)
    con.register("right_raw", right_tbl)

    # Left proj: one row per DISTINCT norm_name — filing multiplicity collapsed.
    # norm_name is the BDC company identity; min() picks a representative raw
    # name and key_source ('clean' sorts before 'dba' when a name matched both).
    con.execute(
        """
        CREATE TEMP TABLE left_proj AS
        SELECT
            norm_name,
            min(bdc_raw_name) AS bdc_raw_name,
            min(key_source)   AS key_source
        FROM left_raw
        WHERE norm_name IS NOT NULL AND norm_name <> ''
        GROUP BY norm_name
        """
    )
    # Right proj: DISTINCT (unique_entity_id, norm_name).
    con.execute(
        """
        CREATE TEMP TABLE right_proj AS
        SELECT DISTINCT unique_entity_id, legal_business_name, norm_name
        FROM right_raw
        WHERE norm_name IS NOT NULL AND norm_name <> ''
        """
    )

    rows_left_proj  = con.execute("SELECT count(*) FROM left_proj").fetchone()[0]
    rows_right_proj = con.execute("SELECT count(*) FROM right_proj").fetchone()[0]
    distinct_left   = con.execute(
        "SELECT count(DISTINCT norm_name) FROM left_proj"
    ).fetchone()[0]
    distinct_right  = con.execute(
        "SELECT count(DISTINCT norm_name) FROM right_proj"
    ).fetchone()[0]
    logger.info(
        "  left_proj rows=%d distinct_keys=%d | right_proj rows=%d distinct_keys=%d",
        rows_left_proj, distinct_left, rows_right_proj, distinct_right,
    )

    logger.info("computing fan-out tables ...")
    # Left fan-out: 1 by construction — left_proj holds exactly one row per
    # distinct norm_name (the BDC company identity). count(*) GROUP BY == 1.
    con.execute(
        """
        CREATE TEMP TABLE left_fanout AS
        SELECT norm_name, count(*) AS left_fo
        FROM left_proj GROUP BY norm_name
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE right_fanout AS
        SELECT norm_name, count(DISTINCT unique_entity_id) AS right_fo
        FROM right_proj GROUP BY norm_name
        """
    )

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            l.norm_name,
            l.bdc_raw_name,
            l.key_source      AS bdc_key_source,
            r.unique_entity_id,
            r.legal_business_name AS sam_legal_business_name,
            lf.left_fo,
            rf.right_fo,
            CASE
                WHEN lf.left_fo > {COLLISION_THRESHOLD}
                  OR rf.right_fo > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN lf.left_fo = 1 AND rf.right_fo = 1
                    THEN 'platinum'
                WHEN lf.left_fo = 1 OR  rf.right_fo = 1
                    THEN 'gold'
                ELSE 'silver'
            END                         AS confidence_tier,
            '{METHOD_NAME}'             AS match_method,
            l.norm_name                 AS match_value,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}'          AS bridge_version,
            '{bridge_run_id}'           AS bridge_run_id
        FROM left_proj l
        JOIN right_proj r USING (norm_name)
        JOIN left_fanout lf USING (norm_name)
        JOIN right_fanout rf USING (norm_name)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
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

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1":   row_counts[1],   # platinum
        "rows_tier2":   row_counts[2],   # gold
        "rows_tier3":   row_counts[3],   # silver
        "rows_collision_rejected": rejected,
        "rows_left_proj":  rows_left_proj,
        "rows_right_proj": rows_right_proj,
        "distinct_left_keys":  distinct_left,
        "distinct_right_keys": distinct_right,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Lance write inside the commit lock; BTREE on norm_name (join key)."""
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
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        # BTREE on the bridge join key (norm_name) — per directive Constraint.
        try:
            ds.create_scalar_index("norm_name", index_type="BTREE", replace=True)
            logger.info("  BTREE on norm_name created")
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
    """Register the NEW match_method + version + bridge rows.

    `bdc_company_name_exact` is a genuinely new method: name-only, no state,
    left side is BDC SOI portfolio companies.  It is DISTINCT from the name+state
    methods (`company_name_state_exact`) and from any existing name-only method.
    Registering a new method_name avoids P4 (shared-method-version-overwrite).
    """
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on canonical normalize_entity_name v1.0.0 of "
            "BDC portfolio company names (pipe-split portfolio_company_name_clean "
            "+ portfolio_company_dba) vs. raw SAM.gov legal_business_name. "
            "Name-only match (no state gate) — BDC SOI has no address/state field."
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
            "distinct (company x UEI) grain; left fan-out is 1 by construction. "
            "platinum=1:1; gold=one name -> 2..50 SAM UEIs; rejected=>50"
        ),
        rejection_rule_description=(
            "a normalized BDC company name resolving to >50 distinct SAM "
            "unique_entity_id values -> rejected"
        ),
        input_columns_left=[
            "portfolio_company_name_clean",
            "portfolio_company_dba",
        ],
        input_columns_right=["legal_business_name"],
        output_value_description=(
            "normalize_entity_name(raw_name) — canonical v1.0.0 applied to "
            "both sides fresh (not trusting pre-materialized columns)"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "BDC Schedule-of-Investments portfolio companies x SAM.gov registered "
            "entities. Name-only match via canonical normalize_entity_name v1.0.0. "
            "Left: pipe-split portfolio_company_name_clean + portfolio_company_dba "
            "(company-typed rows only, sector-header blacklisted). Right: FULL "
            "~884K SAM entities (no award filter). UEI resolved via unique_entity_id."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply",   action="store_true", help="write Lance + registry rows")
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

    logger.info(
        "bridge: %s  method=%s v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER,
    )
    logger.info("  left:   %s", SOI_LANCE_URI)
    logger.info("  right:  %s", SAM_LANCE_URI)
    logger.info("  output: %s", BRIDGE_LANCE_URI)
    logger.info("  normalizer: entity_name_normalize v%s", NORMALIZER_VERSION)

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
        soi_arrow, sam_arrow, rows_left_raw, rows_right_raw = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            soi_arrow,
            sam_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        platinum_gold = counts["rows_tier1"] + counts["rows_tier2"]

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1:1):         %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (1:N | N:1):   %s", f"{counts['rows_tier2']:,}")
        logger.info(
            "    silver   (N:M <=%d):    %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected:  %s", f"{counts['rows_collision_rejected']:,}"
        )
        logger.info("  platinum+gold:           %s", f"{platinum_gold:,}")
        logger.info("  HARD-FAIL floor:         %s", f"{MIN_ROWS_PLATINUM_GOLD:,}")

        # HARD-FAIL check — bridge aborts if platinum+gold < floor.
        if platinum_gold < MIN_ROWS_PLATINUM_GOLD:
            msg = (
                f"HARD FAIL: platinum+gold={platinum_gold:,} < "
                f"floor={MIN_ROWS_PLATINUM_GOLD:,}"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        # Tier-sanity check: among NON-REJECTED rows, platinum+gold must be majority.
        non_rejected = counts["rows_matched"]
        if non_rejected > 0:
            pg_pct = platinum_gold / non_rejected
            logger.info(
                "  tier sanity (non-rejected): platinum+gold=%d/%d (%.1f%%)",
                platinum_gold, non_rejected, pg_pct * 100,
            )
            if pg_pct <= 0.5:
                logger.warning(
                    "  tier-sanity warning: platinum+gold is NOT the majority of "
                    "non-rejected rows (%.1f%%) — review blacklist / normalizer",
                    pg_pct * 100,
                )

        if args.dry_run:
            logger.info(
                "DRY RUN — no Lance / Postgres writes.  duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left":  rows_left_raw,
                "rows_right": rows_right_raw,
                "rows_matched":           counts["rows_matched"],
                "rows_tier1":             counts["rows_tier1"],
                "rows_tier2":             counts["rows_tier2"],
                "rows_tier3":             counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows":             lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  lance_rows=%d  duration=%.1fs",
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
