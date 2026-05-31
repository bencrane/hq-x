#!/usr/bin/env python3
"""Lance-on-R2 bridge generator: SAM entity ↔ PDL company via website domain.

Lance variant of build_bridge_sam_pdl_domain.py (the Parquet bridge). Reads
the canonical Lance inputs sam_gov/entities_lance + pdl/free_companies_lance
via PyLance scanners, joins on normalized website domain, fans out into
tier (platinum/gold/silver/rejected), writes a Lance dataset at
polaris-warehouse/bridges/sam_pdl_domain_lance with a BTREE index on uei.

Pattern: Arrow-bridge (lance.dataset.scanner → arrow → DuckDB tables → join
→ Lance write). Modeled on build_bridge_ucc_pdl_lance.py but SINGLE-PATH:
no GLEIF parent layer, just direct domain-exact match.

Domain normalization SQL is IDENTICAL to the Parquet bridge:
    lower → trim → strip '^https?://' → strip '^www\\.' → strip '/.*$'
    validate via regex ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$
        AND not numeric-only

KEY DIFFERENCE vs FMCSA-PDL / Parquet sam_pdl_domain: SAM entity_url is
always a business domain (no free-mail filtering needed). SAM-PDL uses
the shared domain_exact v1.0.0 method WITHOUT applying the free-mail
blacklist. Per directive review: do NOT call register_match_method /
register_match_method_version — those rows are shared with FMCSA-PDL +
legacy Parquet sam_pdl_domain; re-registering with different config
(input_columns_left=['entity_url']) would clobber FMCSA-PDL's provenance.
Match the Parquet bridge's precedent: import only register_bridge +
start/complete/fail_bridge_run.

Output: polaris-warehouse/bridges/sam_pdl_domain_lance
        (additive — legacy Parquet at bridges/sam_pdl_domain/snapshot=*/
        data.parquet is UNTOUCHED).

Floor: ≥ 250,000 rows (≈78% of legacy Parquet bridge 320,864 — 22%
headroom for any Python-vs-DuckDB regex drift).

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_sam_pdl_domain_lance.py --apply

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_sam_pdl_domain_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sam_pdl_domain_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sam_pdl_domain_lance"  # distinct from legacy Parquet "sam_pdl_domain"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_domain_lance"
DATASET_SLUG = "sam_pdl_domain_lance"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected
# Floor: ≈78% of legacy Parquet bridge 320,864 — 22% headroom for Python /
# DuckDB regex drift. HARD FAIL if rows_matched < MIN_ROWS_MATCHED.
MIN_ROWS_MATCHED = 250_000
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


def _normalize_domain_sql(raw_expr: str) -> str:
    """Domain normalization SQL — IDENTICAL to build_bridge_sam_pdl_domain.py."""
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _domain_validation_sql(col: str) -> str:
    """Validation predicate: well-formed DNS shape AND not numeric-only."""
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read SAM + PDL Lance datasets via PyLance scanner with column projection."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_arrow = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_state_normalized",
            "entity_url",
        ],
        filter=pc.field("entity_url").is_valid(),
    ).to_table()
    rows_sam = len(sam_arrow)
    logger.info("  sam entities (entity_url IS NOT NULL): %d rows", rows_sam)

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "pdl_website",
            "pdl_industry",
            "pdl_size",
            "pdl_locality",
            "pdl_linkedin_url",
        ]
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return sam_arrow, pdl_arrow, rows_sam, rows_pdl


def _build_match_table(
    sam_arrow,
    pdl_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize → fan-out → tier; populate TEMP TABLE bridge_match."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("sam_raw", sam_arrow)
    con.register("pdl_raw", pdl_arrow)

    sam_domain_expr = _normalize_domain_sql("entity_url")
    pdl_domain_expr = _normalize_domain_sql("pdl_website")
    validate_sam = _domain_validation_sql("normalized_domain")
    validate_pdl = _domain_validation_sql("normalized_domain")

    logger.info("materializing sam_branded + pdl_validated ...")
    con.execute(
        f"""
        CREATE TEMP TABLE sam_branded AS
        WITH sam AS (
            SELECT
                unique_entity_id AS uei,
                legal_business_name AS sam_legal_name,
                physical_address_state_normalized AS sam_state,
                entity_url AS sam_url_raw,
                {sam_domain_expr} AS normalized_domain
            FROM sam_raw
            WHERE entity_url IS NOT NULL AND entity_url != ''
        )
        SELECT *
          FROM sam
         WHERE normalized_domain IS NOT NULL
           AND {validate_sam}
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE pdl_validated AS
        WITH pdl AS (
            SELECT
                pdl_id AS pdl_company_id,
                pdl_name,
                pdl_website AS pdl_website_raw,
                {pdl_domain_expr} AS normalized_domain,
                pdl_industry,
                pdl_size AS pdl_size_bucket,
                pdl_locality,
                pdl_linkedin_url
            FROM pdl_raw
            WHERE pdl_website IS NOT NULL
        )
        SELECT *
          FROM pdl
         WHERE normalized_domain IS NOT NULL
           AND {validate_pdl}
        """
    )

    rows_sam_valid = con.execute("SELECT count(*) FROM sam_branded").fetchone()[0]
    rows_pdl_valid = con.execute("SELECT count(*) FROM pdl_validated").fetchone()[0]
    logger.info(
        "  sam_branded: %s | pdl_validated: %s",
        f"{rows_sam_valid:,}", f"{rows_pdl_valid:,}",
    )

    logger.info("computing per-domain fan-out + tiered join ...")
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT normalized_domain, count(*) AS sam_entities_at_domain
        FROM sam_branded
        GROUP BY normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE pdl_fanout AS
        SELECT normalized_domain, count(*) AS pdl_companies_at_domain
        FROM pdl_validated
        GROUP BY normalized_domain
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            s.uei,
            p.pdl_company_id,
            '{METHOD_NAME}' AS match_method,
            s.normalized_domain AS match_value,
            sf.sam_entities_at_domain,
            pf.pdl_companies_at_domain,
            CASE
                WHEN sf.sam_entities_at_domain > {COLLISION_THRESHOLD}
                  OR pf.pdl_companies_at_domain > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_entities_at_domain = 1
                  AND pf.pdl_companies_at_domain = 1
                    THEN 'platinum'
                WHEN sf.sam_entities_at_domain = 1
                  OR pf.pdl_companies_at_domain = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM sam_branded s
        JOIN pdl_validated p USING (normalized_domain)
        JOIN sam_fanout    sf ON sf.normalized_domain = s.normalized_domain
        JOIN pdl_fanout    pf ON pf.normalized_domain = s.normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            uei, pdl_company_id, match_method, match_value,
            confidence_tier, sam_entities_at_domain, pdl_companies_at_domain,
            generated_at, bridge_version, bridge_run_id
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
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
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Lance write inside the commit lock; create BTREE on uei."""
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

        try:
            ds.create_scalar_index("uei", index_type="BTREE", replace=True)
            logger.info("  BTREE on uei created")
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
    """Register ONLY the new bridge_name row in ops.bridges.

    IMPORTANT (per Stage 3.B review): do NOT call register_match_method or
    register_match_method_version — the `domain_exact` rule + its 1.0.0
    version row are SHARED with the FMCSA-PDL bridge and the legacy Parquet
    sam_pdl_domain bridge. The helpers do idempotent UPSERTs ON CONFLICT
    DO UPDATE, so calling register_match_method_version with our config
    (e.g. input_columns_left=['entity_url']) would OVERWRITE the shared
    fields used by FMCSA-PDL (input_columns_left=['EMAIL_ADDRESS'], etc.)
    and break their provenance trail.

    Precedent: build_bridge_sam_pdl_domain.py (Parquet variant) imports
    ONLY start_bridge_run / complete_bridge_run / fail_bridge_run for
    exactly this reason. register_bridge is safe because bridge_name
    'sam_pdl_domain_lance' is NEW.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM entities × PDL companies via website domain (Lance variant; "
            "additive — legacy Parquet bridge sam_pdl_domain remains at "
            "bridges/sam_pdl_domain/snapshot=*/data.parquet)."
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
    logger.info("inputs: %s + %s (Arrow-bridge)", SAM_LANCE_URI, PDL_LANCE_URI)
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
        sam_arrow, pdl_arrow, rows_sam, rows_pdl = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_arrow,
            pdl_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1:1):         %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (1:N | N:1):   %s", f"{counts['rows_tier2']:,}")
        logger.info(
            "    silver   (N:M ≤%d):    %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected:  %s", f"{counts['rows_collision_rejected']:,}"
        )

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
            logger.info(
                "DRY RUN — no Lance / Postgres writes. duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_sam,
                "rows_right": rows_pdl,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,  # SAM entity_url is always business-domain
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
