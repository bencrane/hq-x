#!/usr/bin/env python3
"""Emit `bridges/sam_pdl_lance` — SAM ∩ PDL match WITHOUT the USAspending filter.

Rationale
---------
The canonical `bridges/sam_pdl_usaspending_lance` carries PDL columns
(linkedin_url, website, industry, ...) but is conceptually "matches that
ALSO have USAspending PRIME contract history" — its sam_pdl_spine step
INNER JOINs SAM × PDL and then LEFT JOINs USAspending rollups; UEIs with
no prime contract history come through but their downstream consumers
(notably ``federal_contractor_profile_pdl_lance``, the cohort emit's
current fast-path source) LEFT-JOIN-start from ``spines/federal_contractor_profile_lance``,
a 101,413-row spine restricted to confirmed prime award winners.

Net effect: a UEI that is registered in SAM, whose domain matches a PDL
record, but that has only ever received subawards (no prime contract of
its own) falls out of the fast-path resolution chain. The fast lane sees
``linkedin_url IS NULL`` and drops the row into slow lane (or dark).

This bridge fixes that. It joins ``bridges/sam_pdl_domain_lance`` (the
canonical UEI ↔ pdl_company_id match table, 320,644 rows post-global-PDL)
to the raw PDL company dataset on ``pdl_id`` and pulls the PDL firmographic
columns through, then LEFT JOINs SAM's `corporate_website` for slow-lane
fallback. Output is the same UEI-grain as sam_pdl_domain — same matching
logic, same collision rules, same tiering — just with PDL columns wide
instead of needing a downstream join.

Schema
------
    uei                          (key, BTREE)
    pdl_id                       (PDL primary key — alias of sam_pdl_domain.pdl_company_id)
    pdl_name
    pdl_website                  (PDL's raw website value — already lowercased, mostly bare)
    pdl_linkedin_url             (fast-lane key for the cohort emit)
    pdl_industry
    pdl_size
    pdl_locality
    pdl_country                  (e.g., 'germany' for DHL — non-US records now flow)
    sam_corporate_website        (SAM's entity_url — slow-lane key when pdl_linkedin_url is NULL)
    sam_legal_business_name
    match_method                 (= 'domain_exact' from sam_pdl_domain)
    confidence_tier              (platinum/gold/silver from sam_pdl_domain)
    sam_entities_at_domain
    pdl_companies_at_domain
    generated_at
    bridge_version
    bridge_run_id

Sources
-------
    bridges/sam_pdl_domain_lance  (320,644 rows — UEI ↔ pdl_company_id match table)
    pdl/free_companies_lance      (34.3M global rows — PDL firmographic source)
    spines/sam_entities_lance     (883K rows — for SAM corporate_website + legal name)

Run
---
    cd apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
      uv run python -m scripts.build_bridge_sam_pdl_lance --apply

    doppler run --project hq-all --config prd -- \\
      uv run python -m scripts.build_bridge_sam_pdl_lance --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("build_bridge_sam_pdl_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sam_pdl_lance"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

# R2 layout ------------------------------------------------------------------
SAM_PDL_DOMAIN_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_domain_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
SAM_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"
)
DATASET_SLUG = "sam_pdl_lance"

# Floor: sam_pdl_domain has 320K rows post-global-PDL. INNER JOIN on pdl_id
# should preserve nearly all rows (PDL is the right side of sam_pdl_domain
# already, so every pdl_company_id in the match table exists in PDL).
# 250K is a generous floor with ~22% headroom for drift.
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


def _open_sources(storage_options: dict):
    """Open all three Lance sources and project the columns we need.

    Returns (sam_pdl_domain_arrow, pdl_arrow, sam_arrow).
    """
    import lance

    LOG.info("opening bridges/sam_pdl_domain_lance ...")
    spd_ds = lance.dataset(SAM_PDL_DOMAIN_URI, storage_options=storage_options)
    spd_arrow = spd_ds.scanner(
        columns=[
            "uei",
            "pdl_company_id",
            "match_method",
            "confidence_tier",
            "sam_entities_at_domain",
            "pdl_companies_at_domain",
        ]
    ).to_table()
    LOG.info("  sam_pdl_domain rows: %d", spd_arrow.num_rows)

    LOG.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "pdl_website",
            "pdl_linkedin_url",
            "pdl_industry",
            "pdl_size",
            "pdl_locality",
            "pdl_country",
        ]
    ).to_table()
    LOG.info("  pdl rows: %d", pdl_arrow.num_rows)

    LOG.info("opening spines/sam_entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_arrow = sam_ds.scanner(
        columns=[
            "uei",
            "corporate_website",
            "legal_business_name",
        ]
    ).to_table()
    LOG.info("  sam entities rows: %d", sam_arrow.num_rows)

    return spd_arrow, pdl_arrow, sam_arrow


def _build_match_table(
    spd_arrow,
    pdl_arrow,
    sam_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """INNER JOIN sam_pdl_domain × pdl ON pdl_id; LEFT JOIN sam ON uei."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.register("spd", spd_arrow)
    con.register("pdl", pdl_arrow)
    con.register("sam", sam_arrow)

    # Dedup SAM on uei first (SAM has historical / re-registration rows).
    # Without this, LEFT JOIN with SAM fans out rows where a UEI has multiple
    # SAM entries — confirmed in dry-run: 330,983 rows vs the 320,644 in
    # sam_pdl_domain. Take the most-recently-activated row per UEI.
    con.execute(
        """
        CREATE TEMP TABLE sam_one_per_uei AS
        SELECT uei, corporate_website, legal_business_name
        FROM (
            SELECT uei, corporate_website, legal_business_name,
                   ROW_NUMBER() OVER (PARTITION BY uei
                                      ORDER BY corporate_website IS NOT NULL DESC,
                                               legal_business_name) AS rn
            FROM sam
            WHERE uei IS NOT NULL
        )
        WHERE rn = 1
        """
    )

    LOG.info("computing join (inner spd × pdl, left sam_dedup) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            spd.uei                                         AS uei,
            spd.pdl_company_id                              AS pdl_id,
            pdl.pdl_name                                    AS pdl_name,
            pdl.pdl_website                                 AS pdl_website,
            pdl.pdl_linkedin_url                            AS pdl_linkedin_url,
            pdl.pdl_industry                                AS pdl_industry,
            pdl.pdl_size                                    AS pdl_size,
            pdl.pdl_locality                                AS pdl_locality,
            pdl.pdl_country                                 AS pdl_country,
            sam.corporate_website                           AS sam_corporate_website,
            sam.legal_business_name                         AS sam_legal_business_name,
            spd.match_method                                AS match_method,
            spd.confidence_tier                             AS confidence_tier,
            spd.sam_entities_at_domain                      AS sam_entities_at_domain,
            spd.pdl_companies_at_domain                     AS pdl_companies_at_domain,
            TIMESTAMP '{generated_at_iso}'                  AS generated_at,
            '{BRIDGE_VERSION}'                              AS bridge_version,
            '{bridge_run_id}'                               AS bridge_run_id
        FROM spd
        INNER JOIN pdl ON pdl.pdl_id = spd.pdl_company_id
        LEFT JOIN sam_one_per_uei sam ON sam.uei = spd.uei
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE NULLIF(TRIM(pdl_linkedin_url), '') IS NOT NULL),
            COUNT(*) FILTER (WHERE NULLIF(TRIM(sam_corporate_website), '') IS NOT NULL),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver'),
            COUNT(*) FILTER (WHERE LOWER(pdl_country) <> 'united states'
                              AND pdl_country IS NOT NULL)
        FROM bridge_match
        """
    ).fetchone()
    counts = {
        "rows_matched":            counts_row[0],
        "rows_with_linkedin":      counts_row[1],
        "rows_with_sam_website":   counts_row[2],
        "rows_tier1_platinum":     counts_row[3],
        "rows_tier2_gold":         counts_row[4],
        "rows_tier3_silver":       counts_row[5],
        "rows_non_us":             counts_row[6],
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance + BTREE on uei."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
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
        LOG.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        LOG.info("BTREE on uei: OK")

        try:
            ds.optimize.compact_files()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("compact_files failed (non-fatal): %s", exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    return lance_count


def _register_polaris() -> None:
    register_or_update_polaris(
        namespace="bridges",
        table_name=DATASET_SLUG,
        s3_uri=BRIDGE_LANCE_URI.rstrip("/") + "/",
        docstring=(
            "SAM ∩ PDL match (UEI ↔ pdl_company_id) with PDL firmographic "
            "columns and SAM corporate_website. No USAspending filter — "
            "captures pure-subawardee UEIs that fall out of sam_pdl_usaspending."
        ),
    )
    LOG.info("polaris registration OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + register Polaris")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = parser.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    t_total = time.time()
    bridge_run_id = str(uuid.uuid4())
    generated_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    LOG.info("=" * 60)
    LOG.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    LOG.info("inputs: sam_pdl_domain + pdl_free_companies + sam_entities (Arrow-bridge)")
    LOG.info("output: %s", BRIDGE_LANCE_URI)
    LOG.info("bridge_run_id=%s", bridge_run_id)

    storage_options = _lance_storage_options()
    spd_arrow, pdl_arrow, sam_arrow = _open_sources(storage_options)
    con, counts = _build_match_table(
        spd_arrow,
        pdl_arrow,
        sam_arrow,
        bridge_run_id=bridge_run_id,
        generated_at_iso=generated_at_iso,
    )

    LOG.info("-" * 60)
    LOG.info("bridge counts:")
    for k, v in counts.items():
        LOG.info("  %-25s %s", k + ":", f"{v:,}")

    if args.dry_run:
        LOG.info("DRY RUN — no Lance / Polaris writes. duration=%.1fs", time.time() - t_total)
        return 0

    if counts["rows_matched"] < MIN_ROWS_MATCHED:
        LOG.error(
            "FLOOR FAIL: rows_matched=%d < MIN_ROWS_MATCHED=%d",
            counts["rows_matched"], MIN_ROWS_MATCHED,
        )
        return 1

    lance_count = _write_bridge_lance(con, storage_options)
    _register_polaris()

    LOG.info("=" * 60)
    LOG.info(
        "OK — run_id=%s  lance_rows=%d  duration=%.1fs",
        bridge_run_id, lance_count, time.time() - t_total,
    )
    LOG.info("output: %s", BRIDGE_LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
