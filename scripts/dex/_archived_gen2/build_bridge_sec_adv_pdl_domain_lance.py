#!/usr/bin/env python3
"""Lance-on-R2 bridge generator: SEC ADV firm ↔ PDL company via website domain.

Pattern B Lance bridge. Reads schedule_d_1i_lance (ADV Schedule D Section 1.I
per-firm website list) × pdl/free_companies_lance, joins on normalized website
domain using the REUSED domain_exact match method (L21 — shared with SAM-PDL,
FMCSA-PDL, USAspending-PDL — do NOT call register_match_method_version).

Source correction (validator note — directive premise was wrong):
    ADV firm website lives in schedule_d_1i_lance.website (one row per
    CRD-website pair; 1,995,392 rows) — NOT base_a_lance.raw_json sub-keys.
    Validator scanned 2,000 base_a rows; found ZERO URL-shaped values in
    any Item-1 sub-key. Schedule D 1.I is the authoritative source.

Domain normalizer (L10/L15): inline SQL block pasted VERBATIM from
    build_bridge_sam_pdl_domain_lance.py:109-118 (canonical etld+1 shape).
    _lib/domain_normalize.py is metadata-only (does not exist on disk).

L17 — every output row carries bridge_run_id UUID.
L21 — REUSE domain_exact; do NOT call register_match_method_version.
L11 — output rows include generated_at + bridge_version.
L50 — ops.data_sources 5-col shape; no 'kind' column.

Pre-match cohort (validator probe):
    ADV distinct CRDs with valid domain: 23,896
    ADV ∩ PDL distinct normalized domains: 21,468
    Post >50 fan-out rejection: 29,594 matched rows
    MIN_ROWS_MATCHED = 20,000 (0.7 × 29,594 — validator-computed floor)

G9 platinum smoke fixture: CRD 159376 / newportwealthstrategies.com (1:1)

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/build_bridge_sec_adv_pdl_domain_lance.py --apply

    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/build_bridge_sec_adv_pdl_domain_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sec_adv_pdl_domain_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sec_adv_pdl_domain_lance"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"    # REUSE existing shared semver — do NOT re-register
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sec_adv_schedule_d_1i_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
ADV_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/schedule_d_1i_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_adv_pdl_domain_lance"
DATASET_SLUG = "sec_adv_pdl_domain_lance"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50   # >50 fan-out on either side → rejected
# Floor: 0.7 × 29,594 validator-measured post-rejection matches ≈ 20,715.
# Rounded down to 20,000 for a 3% extra safety margin.
# HARD FAIL if rows_matched < MIN_ROWS_MATCHED.
MIN_ROWS_MATCHED = 20_000
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
    """Domain normalization SQL — IDENTICAL to build_bridge_sam_pdl_domain_lance.py:109-118."""
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
    """Read ADV schedule_d_1i_lance + PDL free_companies_lance via PyLance scanner."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening sec_adv/schedule_d_1i_lance ...")
    adv_ds = lance.dataset(ADV_LANCE_URI, storage_options=storage_options)
    adv_arrow = adv_ds.scanner(
        columns=["crd_number", "website"],
        filter=pc.field("website").is_valid(),
    ).to_table()
    rows_adv = len(adv_arrow)
    logger.info("  schedule_d_1i (website IS NOT NULL): %d rows", rows_adv)

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

    return adv_arrow, pdl_arrow, rows_adv, rows_pdl


def _build_match_table(
    adv_arrow,
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
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='200GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("adv_raw", adv_arrow)
    con.register("pdl_raw", pdl_arrow)

    adv_domain_expr = _normalize_domain_sql("website")
    pdl_domain_expr = _normalize_domain_sql("pdl_website")
    validate_adv = _domain_validation_sql("normalized_domain")
    validate_pdl = _domain_validation_sql("normalized_domain")

    logger.info("materializing adv_branded + pdl_validated ...")
    # ADV schedule_d_1i has one row per CRD-website pair; many CRDs file
    # multiple websites (e.g. JPMorgan filed 3+). Deduplicating to DISTINCT
    # (crd_number, normalized_domain) before the fan-out join prevents the
    # cross-product from expanding to ~1.99M × ~5.8M rows. The validator
    # probe worked on 85,610 distinct (crd, domain) pairs producing 29,594
    # matched rows post-rejection.
    con.execute(
        f"""
        CREATE TEMP TABLE adv_branded AS
        WITH adv AS (
            SELECT
                crd_number,
                {adv_domain_expr} AS normalized_domain
            FROM adv_raw
            WHERE website IS NOT NULL AND website != ''
        )
        SELECT DISTINCT crd_number, normalized_domain
          FROM adv
         WHERE normalized_domain IS NOT NULL
           AND {validate_adv}
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

    rows_adv_valid = con.execute("SELECT count(*) FROM adv_branded").fetchone()[0]
    rows_pdl_valid = con.execute("SELECT count(*) FROM pdl_validated").fetchone()[0]
    logger.info(
        "  adv_branded: %s | pdl_validated: %s",
        f"{rows_adv_valid:,}", f"{rows_pdl_valid:,}",
    )

    logger.info("computing per-domain fan-out + tiered join ...")
    con.execute(
        """
        CREATE TEMP TABLE adv_fanout AS
        SELECT normalized_domain, count(*) AS adv_fan_out
        FROM adv_branded
        GROUP BY normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE pdl_fanout AS
        SELECT normalized_domain, count(*) AS pdl_fan_out
        FROM pdl_validated
        GROUP BY normalized_domain
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            a.crd_number,
            p.pdl_company_id,
            '{METHOD_NAME}' AS match_method,
            a.normalized_domain AS match_value,
            af.adv_fan_out,
            pf.pdl_fan_out,
            CASE
                WHEN af.adv_fan_out > {COLLISION_THRESHOLD}
                  OR pf.pdl_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN af.adv_fan_out = 1
                  AND pf.pdl_fan_out = 1
                    THEN 'platinum'
                WHEN af.adv_fan_out = 1
                  OR pf.pdl_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM adv_branded a
        JOIN pdl_validated p USING (normalized_domain)
        JOIN adv_fanout    af ON af.normalized_domain = a.normalized_domain
        JOIN pdl_fanout    pf ON pf.normalized_domain = a.normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            crd_number, pdl_company_id, match_method, match_value,
            confidence_tier, adv_fan_out, pdl_fan_out,
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
    """Lance write inside the commit lock; create BTREE on crd_number."""
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
            ds.create_scalar_index("crd_number", index_type="BTREE", replace=True)
            logger.info("  BTREE on crd_number created")
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

    IMPORTANT (L21): do NOT call register_match_method or
    register_match_method_version — the `domain_exact` rule + its 1.0.0
    version row are SHARED with the FMCSA-PDL bridge and the SAM-PDL bridge.
    Calling register_match_method_version with our config would OVERWRITE the
    shared fields used by FMCSA-PDL (input_columns_left=['EMAIL_ADDRESS'])
    and break their provenance trail.

    G10 regression baseline hash: 1263541d00a46cf90a549d2494d585b6.
    Calling register_match_method_version here would clobber the row and
    cause G10 to fail with a different hash.

    Precedent: build_bridge_sam_pdl_domain_lance.py:369-397 imports ONLY
    register_bridge + start/complete/fail_bridge_run. Mirror exactly.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SEC Form ADV firms × PDL companies via website domain "
            "(Schedule D 1.I × free_companies_lance; shares domain_exact "
            "method with SAM-PDL, FMCSA-PDL — REUSE not overwrite per L21)."
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
    logger.info("inputs: %s + %s (Arrow-bridge)", ADV_LANCE_URI, PDL_LANCE_URI)
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
        adv_arrow, pdl_arrow, rows_adv, rows_pdl = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            adv_arrow,
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
                "rows_left": rows_adv,
                "rows_right": rows_pdl,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,  # >50 fan-out naturally rejects free-mail
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
