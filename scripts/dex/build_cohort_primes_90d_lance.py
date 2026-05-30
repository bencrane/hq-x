"""Materialize the GTM "active primes — 90d" cohort, split by `pdl_linkedin_url` presence.

Source:
    bridges/federal_contractor_profile_pdl_lance  (101K rows, BTREE on uei)
      — pre-aggregated firmographic profile with 30/90/180/365-day obligation
        rollups + `pdl_linkedin_url` + `entity_url`.

Cohort gate:
    obligation_90d_usd > 50000  (cast through TRY_CAST(... AS DOUBLE) for safety)
  AND
    uei NOT IN (
        SELECT uei FROM hq-x ops.task_runs
        WHERE status IN ('completed','failed','not_found') AND uei IS NOT NULL
    )

Emit (mode="overwrite" — re-runnable):
    s3://dex-raw-landing-zone/polaris-warehouse/cohorts/primes_90d_fast
        → uei, domain, linkedin_url   (rows with pdl_linkedin_url IS NOT NULL)
    s3://dex-raw-landing-zone/polaris-warehouse/cohorts/primes_90d_slow
        → uei, domain                  (rows with pdl_linkedin_url IS NULL)

Each emit:
  - BTREE scalar index on `uei` (Pattern A convention — every load-bearing
    resolution key gets a hard BTREE; the Trigger.dev fan-out keys by uei).
  - Polaris Generic Table registration in the `cohorts` namespace.

Required env (Doppler hq-all/prd):
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    HQX_DB_URL_POOLED  (hq-x Supabase pooled connection),
    POLARIS_PUBLIC_URL, POLARIS_ROOT_PRINCIPAL_ID,
    POLARIS_ROOT_PRINCIPAL_SECRET, POLARIS_DEFAULT_CATALOG_NAME.

Usage:
    cd apps/data-engine-x
    doppler run -p hq-all -c prd -- uv run python scripts/build_cohort_primes_90d_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

# Project-relative import boilerplate (mirrors build_bridge_* scripts).
_THIS = Path(__file__).resolve()
_DEX_ROOT = _THIS.parent.parent
if str(_DEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEX_ROOT))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_cohort_primes_90d")

SOURCE_FPDS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "usaspending/transaction_fpds_lance"
)
SOURCE_SUBAWARD_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "usaspending/subaward_lance"
)
# bridges/sam_pdl_lance — SAM ∩ PDL match table, no USAspending-prime
# filter. 320,644 rows, 100% have pdl_linkedin_url AND sam_corporate_website.
# Replaces federal_contractor_profile_pdl_lance as the cohort emit's
# resolution source: fcp filtered to ~101K confirmed prime award winners,
# which silently dropped pure-subawardee UEIs from fast/slow eligibility
# regardless of PDL match. sam_pdl_lance fixes that — subawardee match rate
# 13.8% → 63.4% on the cohort validation queries.
SOURCE_SAM_PDL_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"
)
# spines/sam_entities_lance — direct SAM source for `corporate_website`
# fallback. Needed because sam_pdl_lance is INNER JOIN with PDL: a UEI is
# in sam_pdl_lance only if it matched PDL. UEIs registered on SAM but
# without a PDL match would lose their `corporate_website` (slow-lane key)
# if we only joined sam_pdl_lance. Direct SAM scan ensures slow-lane
# UEIs still flow through with their SAM-registered website.
SOURCE_SAM_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance"
)
# bridges/fmcsa_sam_legal_name_state_lance — FMCSA × SAM legal-name+state
# match (PR #774). ~89K distinct UEIs (platinum + gold + silver, rejected
# tier filtered at write time). UNION'd into the cohort regardless of
# 90d USAspending activity — many FMCSA-active carriers are SAM-registered
# federal-contracting candidates without an open obligation in the window
# yet. Of the ~89K, ~39K reach a pdl_linkedin_url via sam_pdl_lance and
# flow to fast lane; remainder route to slow/dark via existing SQL.
SOURCE_FMCSA_SAM_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
    "fmcsa_sam_legal_name_state_lance"
)
# bridges/sam_overture_lance — SAM × Overture address-based bridge (PR #776).
# 3,601 LLM-confirmed UEI → Overture place matches (platinum + gold only;
# silver tier intentionally empty per the conservative-bias prompt).
# Source of `website_primary` for previously-dark UEIs whose SAM address
# resolves to an Overture place. COALESCE'd as fallback to SAM's own
# corporate_website in the slow-lane key (domain). For fast lane, these
# UEIs only fall through if a Parallel.ai linkedin exists in ops.task_runs.
SOURCE_SAM_OVERTURE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_overture_lance"
)
# cohorts/sbir_pdl_apex_match_lance — SBIR firms whose apex domain matched
# a PDL company row (8,239 distinct UEIs with pre-resolved PDL linkedin_url).
# Built one-shot from operator's /tmp/sbir_x_pdl_apex_match.csv. UNION'd
# into the cohort regardless of 90d USAspending activity — many SBIR R&D
# firms are SAM-registered federal-contracting candidates without a
# fresh prime award in the window. Carries (uei, domain, linkedin_url);
# linkedin_source tagged 'sbir_pdl_apex' downstream so it can be filtered
# at the Trigger.dev cohort fetch.
SOURCE_SBIR_PDL_APEX_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/sbir_pdl_apex_match_lance"
)
# cohorts/won_365d_pdl_gap_lance — UEIs that won $ in last 365d via
# FPDS or subaward, are SAM-active (activation in last year) with
# entity_url, and have a PDL match (linkedin known). 24K rows seeded
# from a one-shot gap analysis. UNION'd to extend the cohort beyond
# the 90d window for high-confidence Hop-2-ready candidates.
SOURCE_WON_365D_PDL_GAP_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/won_365d_pdl_gap_lance"
)
# cohorts/won_365d_no_pdl_lance — 4,622 UEIs that won $ in last 365d
# but DO NOT have a PDL match. Have SAM entity_url so a normalized
# domain exists. Route to slow lane (no linkedin yet) → Modal cascade
# does Blitz Hop 1 (domain→linkedin) + Hop 2 (linkedin→firmo) in one
# call per UEI. Avoids Parallel.ai spend (Blitz is unlimited).
SOURCE_WON_365D_NO_PDL_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/won_365d_no_pdl_lance"
)

COHORT_FAST_SLUG = "primes_90d_fast"
COHORT_SLOW_SLUG = "primes_90d_slow"
COHORT_FAST_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/primes_90d_fast"
)
COHORT_SLOW_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/primes_90d_slow"
)

# Cohort definition (revision 2026-05-27): UNION of three sources, all
# scoped to the last 90 days:
#   1. FPDS primes where SUM(federal_action_obligation) > MIN_OBLIGATION
#   2. Subaward recipients where SUM(subaward_amount) > MIN_OBLIGATION
#   3. UEIs with ANY modification event (any $$) — captures continuing-
#      contract activity that doesn't clear the obligation floor
#
# Floor dropped from $50K → $0 to maximize the addressable pool.
# Anti-join still excludes terminal-status UEIs from ops.task_runs.
AWARD_RECENCY_DAYS = 90
MIN_OBLIGATION_90D_USD = 0

TMP_DIR = "/tmp/lance"


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _fetch_excluded_ueis() -> list[str]:
    """Pull the exclusion set from hq-x ``ops.task_runs``.

    Terminal-state rows we do NOT want to re-enrich, scoped to the
    Blitz Hop 2 firmographic enrichment task_type only:
        task_type = 'modal_hydrate_firmo_cascade'
        AND status IN ('completed','failed','not_found')

    Narrowed from "any terminal task_type" because other task_types
    (notably ``parallel_domain_to_linkedin`` — Hop 1 LinkedIn URL
    resolution) are NOT firmographic enrichment and must not exclude
    a UEI from the next firmographic pass.

    The hq-x migration ``20260526T190000_add_uei_to_task_runs.sql`` created
    a BTREE on ``ops.task_runs(uei)``; this query is index-supported.
    """
    import psycopg
    db_url = os.environ["HQX_DB_URL_POOLED"]
    t0 = time.perf_counter()
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT uei
                FROM ops.task_runs
                WHERE task_type IN (
                          'modal_hydrate_firmo_cascade',
                          'blitz_firmo_direct'
                      )
                  AND status IN ('completed','failed','not_found')
                  AND uei IS NOT NULL
                """,
            )
            ueis = [row[0] for row in cur.fetchall()]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "fetched %d excluded UEIs from hq-x ops.task_runs in %dms",
        len(ueis), elapsed_ms,
    )
    return ueis


def _fetch_hop1_resolved_linkedins() -> list[tuple[str, str, str, str]]:
    """Pull (uei, domain, linkedin_url, source) from every Hop-1 task_type.

    Covers all non-PDL providers that have written domain → linkedin
    results to ``ops.task_runs``:
        * ``parallel_domain_to_linkedin``  (PR #772 — Parallel.ai)
        * ``clay_domain_to_linkedin``      (entities.clay_find_companies backfill)
        * ``trigger_blitz_domain_to_linkedin`` (PR #781 — Trigger.dev / Blitz)

    Critically, we pull **the actual ``domain`` column** alongside
    ``linkedin_url`` so the cohort emit carries the input domain we sent
    to the resolver — not a SAM-derived fallback. For non-SAM-sourced
    UEIs (Overture-discovered, FMCSA-SAM-bridged, sam_active midtier
    `entity_url_normalized`), SAM's `corporate_website` is NULL and any
    join against it would lose the domain we already know. The previous
    helper only returned (uei, linkedin_url), causing 1,884 fast-lane
    rows to emit with domain=NULL because their domains came from a
    non-SAM cohort source.

    The ``source`` label is derived from the task_type for downstream
    attribution; the cohort emits a ``linkedin_source`` column so Modal's
    cascade and the firmo ledger can know which provider resolved each
    LinkedIn URL.
    """
    import psycopg

    db_url = os.environ["HQX_DB_URL_POOLED"]
    t0 = time.perf_counter()
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    uei,
                    domain,
                    linkedin_url,
                    CASE task_type
                        WHEN 'parallel_domain_to_linkedin'     THEN 'parallel'
                        WHEN 'clay_domain_to_linkedin'         THEN 'clay'
                        WHEN 'trigger_blitz_domain_to_linkedin' THEN 'trigger_blitz'
                    END AS source
                FROM ops.task_runs
                WHERE task_type IN (
                          'parallel_domain_to_linkedin',
                          'clay_domain_to_linkedin',
                          'trigger_blitz_domain_to_linkedin'
                      )
                  AND status = 'completed'
                  AND uei IS NOT NULL
                  AND linkedin_url IS NOT NULL
                  AND domain IS NOT NULL
                """,
            )
            rows = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    by_source: dict[str, int] = {}
    for _, _, _, s in rows:
        by_source[s] = by_source.get(s, 0) + 1
    logger.info(
        "fetched %d hop1-resolved (uei, domain, linkedin, source) from hq-x "
        "ops.task_runs in %dms  by source: %s",
        len(rows), elapsed_ms, by_source,
    )
    return rows


def _scan_fpds_window():
    """Scan FPDS for the last 90 days via action_date BTREE pushdown.

    The dataset has 107M rows; pushdown narrows to ~350K rows in window.
    Includes `modification_number` and `action_type` so the union cohort
    can identify mod-events (UEIs touching an existing contract, any $$).
    """
    import lance
    import pyarrow.compute as pc
    from datetime import date, timedelta

    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_FPDS_URI, storage_options=so)
    window_lo = (date.today() - timedelta(days=AWARD_RECENCY_DAYS)).isoformat()
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=[
            "recipient_uei",
            "action_date",
            "federal_action_obligation",
            "modification_number",
            "action_type",
        ],
        filter=(pc.field("action_date") >= window_lo),
    ).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "fpds scan: %d rows in [%s, today], %dms",
        tbl.num_rows, window_lo, elapsed_ms,
    )
    return tbl


def _scan_subaward_window():
    """Scan subaward_lance for the last 90 days via sub_action_date pushdown.

    Subawards are how mid-tier contractors get federal money without
    appearing as FPDS prime recipients. The previous cohort missed them
    entirely (FPDS-only). Adding them adds ~7.7K net-new UEIs.
    """
    import lance
    import pyarrow.compute as pc
    from datetime import date, timedelta

    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_SUBAWARD_URI, storage_options=so)
    window_lo = (date.today() - timedelta(days=AWARD_RECENCY_DAYS)).isoformat()
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=["sub_awardee_or_recipient_uei", "sub_action_date", "subaward_amount"],
        filter=(pc.field("sub_action_date") >= window_lo),
    ).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "subaward scan: %d rows in [%s, today], %dms",
        tbl.num_rows, window_lo, elapsed_ms,
    )
    return tbl


def _scan_sam_pdl_keys():
    """Scan the SAM ∩ PDL bridge for the fast-lane key (pdl_linkedin_url).

    sam_pdl_lance carries `pdl_linkedin_url` for every UEI matched in
    SAM ∩ PDL, regardless of USAspending PRIME contract history.
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_SAM_PDL_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "pdl_linkedin_url"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "sam_pdl scan: %d rows, %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_sam_keys():
    """Scan SAM entities for the slow-lane key (corporate_website).

    Returns a DEDUPED projection (uei + corporate_website) — one row per
    UEI, preferring the row that has a non-null corporate_website.
    SAM has historical re-registrations per UEI; cohort grain is
    one-per-UEI so we dedup at scan time.
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_SAM_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "corporate_website"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "sam scan: %d rows (pre-dedup), %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_fmcsa_sam_keys():
    """Scan fmcsa_sam_legal_name_state_lance for UEIs to UNION into cohort.

    Returns distinct UEIs only — the bridge has multiple rows per UEI
    (one per matched DOT); cohort grain is one-per-UEI. Confidence-tier
    filter is NOT applied here (operator's directive — admit all
    non-rejected matches; bridge writer already excludes 'rejected').
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_FMCSA_SAM_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "fmcsa_sam scan: %d rows (pre-distinct), %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_sam_overture_keys():
    """Scan sam_overture_lance for fallback slow-lane key (website_primary).

    Returns (uei, website_primary) — one row per UEI by design (the
    bridge picks a single Overture place per UEI). Fallback when SAM's
    own corporate_website is empty.
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_SAM_OVERTURE_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "website_primary"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "sam_overture scan: %d rows, %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_sbir_pdl_apex():
    """Scan SBIR×PDL apex-match Lance for the UNION + linkedin/domain.

    Carries (uei, domain, linkedin_url) — domain is the apex match key
    (pre-normalized), linkedin_url is pdl_linkedin_url from PDL. UEIs
    UNION'd into the cohort even without 90d primes activity (the
    SBIR firm population is largely R&D and doesn't necessarily clear
    that gate). Dedup-on-uei happens downstream in DuckDB.
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_SBIR_PDL_APEX_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain", "linkedin_url"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "sbir_pdl_apex scan: %d rows, %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_won_365d_pdl_gap():
    """Scan won-365d-PDL-gap Lance for the UNION + linkedin/domain.

    Carries (uei, domain, linkedin_url) — UEIs that won $ in last 365d
    AND are SAM-active w/ entity_url AND have PDL linkedin. Extends the
    cohort beyond the 90d window to cover the broader 365d federally-
    active universe (24K rows from gap analysis).
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_WON_365D_PDL_GAP_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain", "linkedin_url"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "won_365d_pdl_gap scan: %d rows, %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _scan_won_365d_no_pdl():
    """Scan won-365d-no-PDL Lance for the UNION (slow-lane seed).

    Carries (uei, domain) only — these UEIs have no linkedin yet. The
    cohort emit's resolved CTE will NULL out linkedin_url for them
    (no sam_pdl hit, no hop1 hit), routing them to the slow lane.
    Modal's gtm_hydration_90d_slow then runs Blitz two-hop (domain →
    linkedin → firmo) per UEI.
    """
    import lance
    so = _r2_storage_options()
    ds = lance.dataset(SOURCE_WON_365D_NO_PDL_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "won_365d_no_pdl scan: %d rows, %dms",
        tbl.num_rows, elapsed_ms,
    )
    return tbl


def _build_duckdb(
    fpds_arrow, sub_arrow, sam_pdl_arrow, sam_arrow,
    fmcsa_sam_arrow, sam_overture_arrow, sbir_pdl_apex_arrow,
    won_365d_pdl_gap_arrow, won_365d_no_pdl_arrow,
    excluded_ueis: list[str],
    hop1_linkedins: list[tuple[str, str, str, str]],
):
    """Register all sources + the anti-join exclusion list + Hop-1-resolved linkedins."""
    import duckdb
    import pyarrow as pa

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='6GB'")
    con.register("fpds", fpds_arrow)
    con.register("sub", sub_arrow)
    con.register("sam_pdl", sam_pdl_arrow)
    con.register("sam_raw", sam_arrow)
    con.register("fmcsa_sam_raw", fmcsa_sam_arrow)
    con.register("sam_overture_raw", sam_overture_arrow)
    con.register("sbir_pdl_apex_raw", sbir_pdl_apex_arrow)
    # sbir_pdl_apex: dedup on uei (CSV had ~75 dupes, Lance write dedups
    # but defensive); prefer rows with non-null linkedin_url.
    con.execute(
        """
        CREATE TEMP TABLE sbir_pdl_apex AS
        SELECT uei, domain, linkedin_url
        FROM (
            SELECT uei, domain, linkedin_url,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY linkedin_url IS NOT NULL DESC,
                                linkedin_url
                   ) AS rn
            FROM sbir_pdl_apex_raw
            WHERE uei IS NOT NULL
        ) WHERE rn = 1
        """
    )
    con.register("won_365d_pdl_gap_raw", won_365d_pdl_gap_arrow)
    con.execute(
        """
        CREATE TEMP TABLE won_365d_pdl_gap AS
        SELECT uei, domain, linkedin_url
        FROM (
            SELECT uei, domain, linkedin_url,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY linkedin_url IS NOT NULL DESC,
                                linkedin_url
                   ) AS rn
            FROM won_365d_pdl_gap_raw
            WHERE uei IS NOT NULL
        ) WHERE rn = 1
        """
    )
    con.register("won_365d_no_pdl_raw", won_365d_no_pdl_arrow)
    con.execute(
        """
        CREATE TEMP TABLE won_365d_no_pdl AS
        SELECT uei, MAX(domain) AS domain
        FROM won_365d_no_pdl_raw
        WHERE uei IS NOT NULL AND domain IS NOT NULL
        GROUP BY uei
        """
    )
    # fmcsa_sam: distinct UEIs only (bridge has multi-DOT rows per UEI).
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_sam AS
        SELECT DISTINCT uei FROM fmcsa_sam_raw WHERE uei IS NOT NULL
        """
    )
    # sam_overture: 1 row per UEI already (bridge writer picks single
    # Overture place per UEI), but defensive dedup in case of upstream
    # drift; prefer rows with non-null website_primary.
    con.execute(
        """
        CREATE TEMP TABLE sam_overture AS
        SELECT uei, website_primary
        FROM (
            SELECT uei, website_primary,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY website_primary IS NOT NULL DESC,
                                website_primary
                   ) AS rn
            FROM sam_overture_raw
            WHERE uei IS NOT NULL
        ) WHERE rn = 1
        """
    )
    # Dedup SAM on uei — one row per UEI, prefer rows with non-null website.
    con.execute(
        """
        CREATE TEMP TABLE sam AS
        SELECT uei, corporate_website
        FROM (
            SELECT uei, corporate_website,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY corporate_website IS NOT NULL DESC,
                                corporate_website
                   ) AS rn
            FROM sam_raw
            WHERE uei IS NOT NULL
        ) WHERE rn = 1
        """
    )
    excl_tbl = pa.table({"uei": pa.array(excluded_ueis, type=pa.string())})
    con.register("excl", excl_tbl)
    # Hop-1-resolved (uei, domain, linkedin_url, source) across every
    # non-PDL provider that's written to ops.task_runs — Parallel.ai,
    # Clay backfill, Trigger.dev/Blitz. The domain comes straight from
    # the row we sent to the resolver; downstream SQL prefers this over
    # SAM's corporate_website (which is NULL for non-SAM-sourced UEIs).
    # Dedup on uei with explicit provider precedence: parallel > clay >
    # trigger_blitz — picks the most-thorough provider's row when a UEI
    # has results from multiple Hop-1 paths (rare, but possible since
    # the orchestrator anti-join joined providers only recently).
    hop1_tbl = pa.table({
        "uei":          pa.array([r[0] for r in hop1_linkedins], type=pa.string()),
        "domain":       pa.array([r[1] for r in hop1_linkedins], type=pa.string()),
        "linkedin_url": pa.array([r[2] for r in hop1_linkedins], type=pa.string()),
        "source":       pa.array([r[3] for r in hop1_linkedins], type=pa.string()),
    })
    con.register("hop1_raw", hop1_tbl)
    con.execute(
        """
        CREATE TEMP TABLE hop1 AS
        SELECT uei, domain, linkedin_url, source
        FROM (
            SELECT uei, domain, linkedin_url, source,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY
                           CASE source
                               WHEN 'parallel'      THEN 1
                               WHEN 'clay'          THEN 2
                               WHEN 'trigger_blitz' THEN 3
                               ELSE 4
                           END,
                           linkedin_url
                   ) AS rn
            FROM hop1_raw
            WHERE uei IS NOT NULL
              AND linkedin_url IS NOT NULL
        ) WHERE rn = 1
        """
    )
    return con


# Cohort = UNION of four populations:
#   (a) FPDS primes with SUM(obligation) > MIN_OBLIGATION in window
#   (b) Subaward recipients with SUM(subaward_amount) > MIN_OBLIGATION in window
#   (c) UEIs with ANY FPDS modification event (any $$)
#   (d) FMCSA × SAM legal-name+state bridge UEIs (~89K SAM-registered
#       FMCSA-active carriers, regardless of 90d USAspending activity — PR #774)
#
# Each UEI then LEFT JOINs to:
#   - sam_pdl_lance      → pdl_linkedin_url       (fast-lane key, primary)
#   - parallel           → parallel.linkedin_url  (fast-lane key, fallback — PR #772/#775)
#   - sam (deduped)      → corporate_website      (slow-lane key, primary)
#   - sam_overture       → website_primary        (slow-lane key, fallback — PR #776)
# then anti-joins terminal-status UEIs from the hq-x ledger (firmographic
# task_type only — PR #773).
_BASE_SQL = f"""
WITH fpds_winners AS (
    SELECT recipient_uei AS uei
    FROM fpds
    WHERE recipient_uei IS NOT NULL
    GROUP BY recipient_uei
    HAVING SUM(TRY_CAST(federal_action_obligation AS DOUBLE)) > {MIN_OBLIGATION_90D_USD}
),
sub_winners AS (
    SELECT sub_awardee_or_recipient_uei AS uei
    FROM sub
    WHERE sub_awardee_or_recipient_uei IS NOT NULL
    GROUP BY sub_awardee_or_recipient_uei
    HAVING SUM(TRY_CAST(subaward_amount AS DOUBLE)) > {MIN_OBLIGATION_90D_USD}
),
mod_events AS (
    SELECT DISTINCT recipient_uei AS uei
    FROM fpds
    WHERE recipient_uei IS NOT NULL
      AND (
        (modification_number IS NOT NULL
         AND modification_number <> ''
         AND modification_number <> '0')
        OR action_type IN ('B','C','D','G','M')
      )
),
cohort AS (
    SELECT uei FROM fpds_winners
    UNION
    SELECT uei FROM sub_winners
    UNION
    SELECT uei FROM mod_events
    UNION
    -- FMCSA × SAM bridge (PR #774): ~89K SAM-registered FMCSA-active
    -- carriers, admitted regardless of 90d USAspending activity. Of these,
    -- ~39K reach pdl_linkedin_url via sam_pdl_lance → fast lane.
    SELECT uei FROM fmcsa_sam
    UNION
    -- SBIR × PDL apex domain match: ~8.2K SBIR R&D firms whose apex
    -- domain matched a PDL company row with pdl_linkedin_url. Admitted
    -- regardless of 90d USAspending activity — SBIR awards drive
    -- multi-year R&D contracts that don't always clear the 90d window.
    -- 100% of these UEIs reach fast lane via sbir_pdl_apex.linkedin_url.
    SELECT uei FROM sbir_pdl_apex
    UNION
    -- Won-365d-PDL gap: 24K UEIs that won $ in last 365d via FPDS or
    -- subaward, are SAM-active w/ entity_url, and have PDL linkedin.
    -- Admitted to widen the cohort to the 365d federally-active window
    -- (the 90d FPDS/sub/mod sources miss this longer tail).
    SELECT uei FROM won_365d_pdl_gap
    UNION
    -- Won-365d-NO-PDL gap: 4,622 UEIs that won $ in last 365d, have
    -- SAM entity_url, but are NOT in PDL bridge AND haven't been
    -- through any hop1 provider yet. Route to slow lane (no linkedin)
    -- → Modal's gtm_hydration_90d_slow does Blitz two-hop per UEI.
    SELECT uei FROM won_365d_no_pdl
),
resolved AS (
    SELECT
        c.uei                                                 AS uei,
        -- Domain resolution order (normalized via canonical pattern —
        -- lower → strip scheme → strip www. → strip path/query/fragment;
        -- matches build_bridge_sam_pdl_domain_lance._normalize_domain_sql):
        --   1. hop1.domain — the actual input we sent to the resolver
        --      (Parallel/Clay/Blitz). Authoritative for non-SAM-sourced
        --      UEIs whose domain came from Overture / FMCSA-SAM /
        --      sam_active midtier — SAM doesn't have a corporate_website
        --      for those, so the SAM fallback would NULL them out.
        --   2. sam.corporate_website (SAM-registered website)
        --   3. sam_overture.website_primary (Overture fallback, PR #776)
        -- Un-normalized inputs caused Blitz Hop 1 to silently drop 540
        -- UEIs (run_cmpnczfrk2u2j0umxfr6a3nvi); normalizing at emit
        -- time prevents the same failure downstream.
        NULLIF(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        lower(trim(
                            COALESCE(
                                NULLIF(TRIM(hop1.domain), ''),
                                NULLIF(TRIM(sbir_pdl_apex.domain), ''),
                                NULLIF(TRIM(sam.corporate_website), ''),
                                NULLIF(TRIM(sam_overture.website_primary), '')
                            )
                        )),
                        '^https?://', ''
                    ),
                    '^www\\.', ''
                ),
                '[/?#].*$', ''
            ),
            ''
        )                                                     AS domain,
        -- LinkedIn URL precedence:
        --   1. sam_pdl bridge (most-curated PDL match)
        --   2. sbir_pdl_apex (PDL via apex-domain match, lighter touch)
        --   3. hop1 (Parallel / Clay / Blitz)
        COALESCE(
            NULLIF(TRIM(sam_pdl.pdl_linkedin_url), ''),
            NULLIF(TRIM(sbir_pdl_apex.linkedin_url), ''),
            NULLIF(TRIM(hop1.linkedin_url), '')
        )                                                     AS linkedin_url,
        -- Attribution: which provider resolved this LinkedIn URL?
        --   'pdl'           — sam_pdl_lance bridge
        --   'sbir_pdl_apex' — SBIR × PDL apex-domain match
        --   <hop1.source>   — 'parallel' | 'clay' | 'trigger_blitz'
        -- NULL when no provider resolved a LinkedIn for this UEI
        -- (slow-lane candidates).
        CASE
            WHEN NULLIF(TRIM(sam_pdl.pdl_linkedin_url), '') IS NOT NULL
                THEN 'pdl'
            WHEN NULLIF(TRIM(sbir_pdl_apex.linkedin_url), '') IS NOT NULL
                THEN 'sbir_pdl_apex'
            WHEN NULLIF(TRIM(hop1.linkedin_url), '') IS NOT NULL
                THEN hop1.source
            ELSE NULL
        END                                                   AS linkedin_source
    FROM cohort c
    LEFT JOIN sam_pdl       ON sam_pdl.uei       = c.uei
    LEFT JOIN sbir_pdl_apex ON sbir_pdl_apex.uei = c.uei
    LEFT JOIN sam           ON sam.uei           = c.uei
    LEFT JOIN sam_overture  ON sam_overture.uei  = c.uei
    LEFT JOIN hop1          ON hop1.uei          = c.uei
),
filtered AS (
    SELECT r.*
    FROM resolved r
    LEFT JOIN excl e ON e.uei = r.uei
    WHERE e.uei IS NULL
)
SELECT * FROM filtered
"""


def _select_fast(con, limit: int | None = None):
    """uei, domain, linkedin_url, linkedin_source — any UEI with a resolved LinkedIn.

    Schema is a strict superset of the prior (uei, domain, linkedin_url) —
    adds ``linkedin_source`` so downstream consumers (Modal cascade, firmo
    ledger) can attribute which provider resolved each URL: ``'pdl'``,
    ``'parallel'``, ``'clay'``, or ``'trigger_blitz'``.

    Optional ``limit`` truncates the emit to the first N rows (no ORDER BY
    — DuckDB picks deterministically per its hash-aggregate plan; cycle
    orchestration relies on the anti-join shrinking the candidate set
    between cycles, not on row order).
    """
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
    SELECT uei, domain, linkedin_url, linkedin_source
    FROM ({_BASE_SQL}) f
    WHERE f.linkedin_url IS NOT NULL
    {limit_clause}
    """
    return con.from_query(sql)


def _select_slow(con, limit: int | None = None):
    """uei, domain — pdl_linkedin_url IS NULL AND domain IS NOT NULL.

    UEIs with neither linkedin_url nor domain are un-enrichable via the
    current Blitz pipeline (no key to feed Hop 1) — drop them from the
    slow cohort rather than write rows Modal will fail on.
    """
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
    SELECT uei, domain
    FROM ({_BASE_SQL}) f
    WHERE f.linkedin_url IS NULL
      AND f.domain IS NOT NULL
    {limit_clause}
    """
    return con.from_query(sql)


def _write_lance(
    duck_rel,
    *,
    slug: str,
    uri: str,
    storage_options: dict[str, str],
) -> int:
    """Write a DuckDB relation to Lance + BTREE on `uei`."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(slug):
        logger.info("writing cohort Lance at %s ...", uri)
        reader = duck_rel.to_arrow_reader(batch_size=50_000)
        ds = lance.write_dataset(
            reader,
            uri,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        row_count = ds.count_rows()
        logger.info(
            "wrote slug=%s rows=%d in %.1fs (version=%s)",
            slug, row_count, write_dur, ds.version,
        )

        ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        logger.info("slug=%s BTREE on uei: OK", slug)

        try:
            ds.optimize.compact_files()
        except Exception as exc:
            logger.warning("slug=%s compact_files failed (non-fatal): %s", slug, exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning(
                "slug=%s cleanup_old_versions failed (non-fatal): %s", slug, exc,
            )
    return row_count


def _register_polaris(slug: str, uri: str, docstring: str) -> None:
    register_or_update_polaris(
        namespace="cohorts",
        table_name=slug,
        s3_uri=uri.rstrip("/") + "/",
        docstring=docstring,
    )
    logger.info("slug=%s Polaris registration: OK", slug)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "GTM active-primes 90d cohort emit (split by PDL LinkedIn-URL "
            "presence) → Lance × 2 in cohorts/ namespace."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run filters, log counts, do NOT write Lance / register Polaris.",
    )
    parser.add_argument(
        "--lane",
        choices=("fast", "slow", "both"),
        default="both",
        help="Which cohort Lane to (re-)emit. Default: both.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Cap rows written to Lance at N per lane (default: unlimited). "
            "Used by the cycle orchestrator to break a large cohort into "
            "Trigger.dev-sized chunks; the next cycle's anti-join naturally "
            "excludes the completed UEIs."
        ),
    )
    args = parser.parse_args()

    storage_options = _r2_storage_options()
    excluded = _fetch_excluded_ueis()
    hop1_linkedins = _fetch_hop1_resolved_linkedins()
    fpds_arrow = _scan_fpds_window()
    sub_arrow = _scan_subaward_window()
    sam_pdl_arrow = _scan_sam_pdl_keys()
    sam_arrow = _scan_sam_keys()
    fmcsa_sam_arrow = _scan_fmcsa_sam_keys()
    sam_overture_arrow = _scan_sam_overture_keys()
    sbir_pdl_apex_arrow = _scan_sbir_pdl_apex()
    won_365d_pdl_gap_arrow = _scan_won_365d_pdl_gap()
    won_365d_no_pdl_arrow = _scan_won_365d_no_pdl()
    con = _build_duckdb(
        fpds_arrow, sub_arrow, sam_pdl_arrow, sam_arrow,
        fmcsa_sam_arrow, sam_overture_arrow, sbir_pdl_apex_arrow,
        won_365d_pdl_gap_arrow, won_365d_no_pdl_arrow,
        excluded, hop1_linkedins,
    )

    fast_rel = _select_fast(con, limit=args.limit)
    slow_rel = _select_slow(con, limit=args.limit)

    fast_count_preview = con.execute(
        f"SELECT COUNT(*) FROM ({_BASE_SQL}) f WHERE f.linkedin_url IS NOT NULL"
    ).fetchone()[0]
    slow_count_preview = con.execute(
        f"SELECT COUNT(*) FROM ({_BASE_SQL}) f WHERE f.linkedin_url IS NULL AND f.domain IS NOT NULL"
    ).fetchone()[0]
    dark_count_preview = con.execute(
        f"SELECT COUNT(*) FROM ({_BASE_SQL}) f WHERE f.linkedin_url IS NULL AND f.domain IS NULL"
    ).fetchone()[0]
    logger.info(
        "preview counts: fast=%d slow=%d dark=%d (sum=%d, excluded_ueis=%d)",
        fast_count_preview, slow_count_preview, dark_count_preview,
        fast_count_preview + slow_count_preview + dark_count_preview,
        len(excluded),
    )

    if args.dry_run:
        print(f"COHORT_FAST_ROW_COUNT: {fast_count_preview}")
        print(f"COHORT_SLOW_ROW_COUNT: {slow_count_preview}")
        return 0

    fast_count: int | None = None
    slow_count: int | None = None

    if args.lane in ("fast", "both"):
        fast_count = _write_lance(
            fast_rel, slug=COHORT_FAST_SLUG, uri=COHORT_FAST_URI,
            storage_options=storage_options,
        )
        _register_polaris(
            COHORT_FAST_SLUG, COHORT_FAST_URI,
            "GTM active-primes 90d cohort — fast lane (PDL LinkedIn-URL resolved).",
        )
    else:
        logger.info("lane=%s — SKIPPING fast lane emit (existing Lance left untouched)", args.lane)

    if args.lane in ("slow", "both"):
        slow_count = _write_lance(
            slow_rel, slug=COHORT_SLOW_SLUG, uri=COHORT_SLOW_URI,
            storage_options=storage_options,
        )
        _register_polaris(
            COHORT_SLOW_SLUG, COHORT_SLOW_URI,
            "GTM active-primes 90d cohort — slow lane (no PDL LinkedIn-URL).",
        )
    else:
        logger.info("lane=%s — SKIPPING slow lane emit (existing Lance left untouched)", args.lane)

    if fast_count is not None:
        print(f"COHORT_FAST_ROW_COUNT: {fast_count}")
    if slow_count is not None:
        print(f"COHORT_SLOW_ROW_COUNT: {slow_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
