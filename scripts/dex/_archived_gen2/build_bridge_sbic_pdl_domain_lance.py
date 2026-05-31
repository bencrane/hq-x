#!/usr/bin/env python3
"""Cycle 1: SBIC × PDL companies via IR-email domain (Pattern B Lance bridge).

Resolves each SBA SBIC Directory fund-manager firm to its PDL company record
via exact-equality on the normalized IR-email domain. Outputs a Lance dataset
at polaris-warehouse/bridges/sbic_pdl_domain_lance with the SBIC fund + PDL
firmographics (name, website, industry, size, locality, LinkedIn).

Pattern B per inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md:
  - inputs: sba.sbic_directory_lance + pdl.free_companies_lance
  - method: domain_exact v1.0.0 (REUSED per L21 — also drives FMCSA-PDL,
    SAM-PDL, etc.) → DO NOT call register_match_method or
    register_match_method_version (would clobber other bridges' provenance)
  - free-mail blacklist applied (per L10) as defense, even though SBIC IR
    emails are corporate domains by nature
  - tiers: platinum=1:1, gold=1:N|N:1, silver=N:M ≤50, rejected=>50 fan-out
  - BTREE on fund_name_normalized

Usage:
  cd apps/data-engine-x && doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sbic_pdl_domain_lance.py --apply

  ... --dry-run for counts only.
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

from scripts._lib.free_mail_domains import FREE_MAIL_DOMAINS  # noqa: E402
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
logger = logging.getLogger("build_bridge_sbic_pdl_domain_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sbic_pdl_domain_lance"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_sbic_directory_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
SBIC_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/sbic_directory_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sbic_pdl_domain_lance"
)
DATASET_SLUG = "sbic_pdl_domain_lance"

# Tier thresholds + floor ----------------------------------------------------
COLLISION_THRESHOLD = 50
# Floor: SBIC has 397 funds with corporate IR domains; PDL has 8.8M companies.
# Conservative floor — even modest match rate (50%) should land 150+; floor
# at 50 leaves 70% headroom for any PDL coverage drift on small-firm domains.
MIN_ROWS_MATCHED = 50
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
    """Domain normalization SQL — IDENTICAL to SAM-PDL / FMCSA-PDL."""
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _domain_validation_sql(col: str) -> str:
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read SBIC + PDL Lance datasets via PyLance scanner with column projection."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening sba/sbic_directory_lance ...")
    sbic_ds = lance.dataset(SBIC_LANCE_URI, storage_options=storage_options)
    sbic_arrow = sbic_ds.scanner(
        columns=[
            "fund_name",
            "fund_name_normalized",
            "manager",
            "manager_name_normalized",
            "state",
            "city",
            "ir_name",
            "ir_email",
            "ir_email_domain",
        ],
        filter=pc.field("ir_email_domain").is_valid(),
    ).to_table()
    rows_sbic = len(sbic_arrow)
    logger.info("  sbic funds (ir_email_domain IS NOT NULL): %d rows", rows_sbic)

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
        ],
        filter=pc.field("pdl_website").is_valid(),
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return sbic_arrow, pdl_arrow, rows_sbic, rows_pdl


def _build_match_table(
    sbic_arrow,
    pdl_arrow,
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
    con.execute("SET preserve_insertion_order=false")
    con.register("sbic_raw", sbic_arrow)
    con.register("pdl_raw", pdl_arrow)

    # Free-mail blacklist (per L10) — SBIC IR domains are corporate by nature
    # but defense-in-depth in case the SBA directory ever has a noreply gmail.
    free_mail_csv = ",".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))
    pdl_domain_expr = _normalize_domain_sql("pdl_website")
    validate_pdl = _domain_validation_sql("normalized_domain")

    logger.info("materializing sbic_branded + pdl_validated ...")
    # SBIC side: ir_email_domain is already extracted + lowercased upstream;
    # validate + drop free-mail.
    con.execute(
        f"""
        CREATE TEMP TABLE sbic_branded AS
        SELECT
            fund_name,
            fund_name_normalized,
            manager,
            manager_name_normalized,
            state AS fund_state,
            city AS fund_city,
            ir_name,
            ir_email,
            lower(trim(ir_email_domain)) AS normalized_domain
        FROM sbic_raw
        WHERE ir_email_domain IS NOT NULL
          AND ir_email_domain != ''
          AND NOT (ir_email_domain IN ({free_mail_csv}))
          AND (lower(trim(ir_email_domain))
               ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$'
               AND NOT (lower(trim(ir_email_domain)) ~ '^[0-9.]+$'))
        """
    )
    sbic_blacklist = (
        len(sbic_arrow)
        - con.execute("SELECT count(*) FROM sbic_branded").fetchone()[0]
    )
    logger.info(
        "  sbic_branded after free-mail blacklist: %d rows (%d dropped)",
        con.execute("SELECT count(*) FROM sbic_branded").fetchone()[0],
        sbic_blacklist,
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
    rows_pdl_valid = con.execute(
        "SELECT count(*) FROM pdl_validated"
    ).fetchone()[0]
    logger.info("  pdl_validated: %s", f"{rows_pdl_valid:,}")

    logger.info("computing per-domain fan-out + tiered join ...")
    con.execute(
        """
        CREATE TEMP TABLE sbic_fanout AS
        SELECT normalized_domain, count(*) AS sbic_funds_at_domain
        FROM sbic_branded
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
            s.fund_name,
            s.fund_name_normalized,
            s.manager,
            s.manager_name_normalized,
            s.fund_state,
            s.fund_city,
            s.ir_name,
            s.ir_email,
            p.pdl_company_id,
            p.pdl_name,
            p.pdl_website_raw AS pdl_website,
            p.pdl_industry,
            p.pdl_size_bucket,
            p.pdl_locality,
            p.pdl_linkedin_url,
            '{METHOD_NAME}' AS match_method,
            s.normalized_domain AS match_value,
            sf.sbic_funds_at_domain,
            pf.pdl_companies_at_domain,
            CASE
                WHEN sf.sbic_funds_at_domain > {COLLISION_THRESHOLD}
                  OR pf.pdl_companies_at_domain > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sbic_funds_at_domain = 1
                  AND pf.pdl_companies_at_domain = 1
                    THEN 'platinum'
                WHEN sf.sbic_funds_at_domain = 1
                  OR pf.pdl_companies_at_domain = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM sbic_branded s
        JOIN pdl_validated p USING (normalized_domain)
        JOIN sbic_fanout sf USING (normalized_domain)
        JOIN pdl_fanout  pf USING (normalized_domain)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT *
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
        "domains_blacklisted": sbic_blacklist,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=10_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, time.time() - t0, ds.version,
        )

        # BTREEs on both join keys + provenance lookup column.
        for col in ("fund_name_normalized", "pdl_company_id", "match_value"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s non-fatal: %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files non-fatal: %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions non-fatal: %s", e)

    return lance_count


def _ensure_registry() -> None:
    """register_bridge ONLY — domain_exact v1.0.0 is shared (L21)."""
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA SBIC Directory × PDL free_companies via IR-email domain. "
            "Resolves the fund-manager firm to PDL firmographics + LinkedIn "
            "URL. Free-mail blacklist applied as defense (SBIC IR domains "
            "are corporate by nature)."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true",
                     help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)",
                BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("inputs: %s + %s", SBIC_LANCE_URI, PDL_LANCE_URI)
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
        sbic_arrow, pdl_arrow, rows_sbic, rows_pdl = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sbic_arrow,
            pdl_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:           %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1:1):       %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (1:N | N:1): %s", f"{counts['rows_tier2']:,}")
        logger.info(
            "    silver   (N:M <=%d):  %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected: %s",
            f"{counts['rows_collision_rejected']:,}",
        )
        logger.info(
            "  domains_blacklisted (sbic): %s",
            f"{counts['domains_blacklisted']:,}",
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
            logger.info("DRY RUN — no writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_sbic,
                "rows_right": rows_pdl,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": counts["domains_blacklisted"],
            },
        )
        logger.info(
            "OK — run_id=%s lance_rows=%d duration=%.1fs",
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
