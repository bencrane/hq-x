"""Parallel.ai Task API (Group) — domain → company_linkedin_url enrichment.

One-shot orchestrator that takes federal-contractor UEIs we couldn't resolve
via Blitz Hop 1 (`domain → linkedin`) and tries again using Parallel.ai's
Task Group API with the `lite` processor. Writes per-UEI results to
``ops.task_runs`` so downstream Blitz Hop 2 (`linkedin → firmographic`) can
re-attempt them in a later cycle.

Five source populations
-----------------------
1. **slow_lane** — UEIs in
   ``cohorts/primes_90d_slow`` (re-emitted with normalized domains).
2. **hop1_retry** — UEIs in ``ops.task_runs`` with
   ``run_id = 'run_cmpnczfrk2u2j0umxfr6a3nvi'`` AND ``status = 'failed'``.
   For these, we look up ``corporate_website`` from
   ``spines/sam_entities_lance`` (deduped one-row-per-UEI), then normalize
   via the canonical SQL.
3. **overture_websites_sam_no_url** — UEIs in
   ``cohorts/overture_websites_sam_no_url_lance``, ≈16.5K net-new entries
   sourced from Overture Places for UEIs without a SAM ``entity_url``.
   Cohort already has normalized domains and has stripped social/marketplace
   junk; we apply an extra in-script filter for ``.gov`` / ``.mil`` /
   ``*.state.*.us`` to drop the handful of government-site rows that leak
   through (a daycare listing ``dhr.state.al.us`` as its website, etc.).
4. **fmcsa_sam_no_pdl** — UEIs in
   ``cohorts/fmcsa_sam_no_pdl_lance``, ≈13.6K FMCSA carriers that joined
   to a SAM entity via legal_name+state, have a ``sam_entity_url``, but
   are NOT in ``bridges/sam_pdl_lance`` (so PDL never produced a
   ``pdl_linkedin_url`` for them). Built specifically to recover the
   stage-2→stage-3 fall-off in the FMCSA × SAM × PDL funnel.
5. **sam_active_no_pdl_midtier** — UEIs in
   ``cohorts/sam_active_no_pdl_midtier_lance``, ≈31.5K active SAM
   entities with federal-dollar history that lack a PDL match. The
   cohort schema carries the canonical ``entity_url_normalized`` (33.7%
   present) plus ``total_365d`` USD obligation; this loader **sorts by
   total_365d desc** so ``--limit N`` enriches the highest-revenue
   UEIs first — important when running under a Parallel.ai credit cap.

The five sets are UNIONed and de-duped on UEI in precedence order
``slow_lane > hop1_retry > overture_websites_sam_no_url > fmcsa_sam_no_pdl
> sam_active_no_pdl_midtier``.

To run only a subset (e.g. enrich the newly-built Overture cohort without
re-spending API credit on UEIs we already processed) pass ``--source
overture``. The orchestrator also anti-joins against existing
``parallel_domain_to_linkedin`` rows in ``ops.task_runs`` so re-runs are
idempotent — UEIs that already have a result row are not re-submitted.

Parallel.ai contract
--------------------
* SDK: ``parallel-web>=0.6.0``  → ``from parallel import Parallel``
* Processor: ``lite`` (NOT ``lite-fast``)
* Input: structured dict per row — ``{"uei": ..., "domain": ...}``
* Output schema: JSON object with a single string field
  ``company_linkedin_url`` (description-grounded, not required)
* Submission: Group API (``client.task_group.create`` →
  ``client.task_group.add_runs`` in chunks of ≤1,000 →
  ``client.task_group.retrieve`` poll loop →
  ``client.task_group.get_runs`` event stream)

Ledger writes (``ops.task_runs``)
---------------------------------
* ``run_id`` — fresh UUID4 (one per orchestrator invocation; all rows share it)
* ``uei`` — input UEI
* ``task_type`` — ``'parallel_domain_to_linkedin'``
* ``status`` — ``'completed'`` if ``company_linkedin_url`` resolves to a
  non-empty string containing ``linkedin.com``; ``'not_found'`` if the
  field is null/empty/whitespace or the URL doesn't look like LinkedIn;
  ``'failed'`` if the Parallel run itself failed (no output).
* ``domain`` — the normalized domain we submitted
* ``linkedin_url`` — the resolved company_linkedin_url (only when completed)
* ``result_payload`` — JSONB ``{"company_linkedin_url": ..., "basis": [...],
  "source": "slow_lane"|"hop1_retry"}``  (basis = Parallel.ai citation list
  for audit; source = which population this UEI came from)

Usage
-----
    cd apps/data-engine-x
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/run_parallel_domain_to_linkedin.py --dry-run
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/run_parallel_domain_to_linkedin.py --apply --limit 10
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/run_parallel_domain_to_linkedin.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Project-relative imports.
_THIS = Path(__file__).resolve()
_DEX_ROOT = _THIS.parent.parent
if str(_DEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEX_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_parallel_domain_to_linkedin")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COHORT_SLOW_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/primes_90d_slow"
)
COHORT_OVERTURE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/overture_websites_sam_no_url_lance"
)
COHORT_FMCSA_SAM_NO_PDL_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/fmcsa_sam_no_pdl_lance"
)
COHORT_SAM_ACTIVE_NO_PDL_MIDTIER_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/sam_active_no_pdl_midtier_lance"
)
SAM_ENTITIES_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance"
)

HOP1_FAILED_RUN_ID = "run_cmpnczfrk2u2j0umxfr6a3nvi"

SOURCE_SLOW_LANE = "slow_lane"
SOURCE_HOP1_RETRY = "hop1_retry"
SOURCE_OVERTURE = "overture_websites_sam_no_url"
SOURCE_FMCSA_SAM_NO_PDL = "fmcsa_sam_no_pdl"
SOURCE_SAM_ACTIVE_NO_PDL_MIDTIER = "sam_active_no_pdl_midtier"
ALL_SOURCES = (
    SOURCE_SLOW_LANE,
    SOURCE_HOP1_RETRY,
    SOURCE_OVERTURE,
    SOURCE_FMCSA_SAM_NO_PDL,
    SOURCE_SAM_ACTIVE_NO_PDL_MIDTIER,
)

PARALLEL_PROCESSOR = "lite"
PARALLEL_ADD_RUNS_CHUNK = 1000  # SDK max per POST
PARALLEL_POLL_INTERVAL_S = 15
PARALLEL_POLL_TIMEOUT_S = 60 * 60 * 2  # 2 hours hard cap

TASK_TYPE = "parallel_domain_to_linkedin"

# All ledger task_types that represent "a domain → linkedin attempt was made
# for this UEI" — used by the idempotence anti-join below so this orchestrator
# never double-spends on UEIs already enriched by another provider. Add new
# task_types here when a sibling pipeline (Clay backfill, Trigger.dev/Blitz
# Hop 1, etc.) lands rows that should suppress a future Parallel.ai re-run.
LEDGER_DOMAIN_TO_LINKEDIN_TASK_TYPES = (
    TASK_TYPE,                           # this script — Parallel.ai
    "clay_domain_to_linkedin",           # Clay backfill (entities.clay_find_companies)
    "trigger_blitz_domain_to_linkedin",  # Trigger.dev Hop 1 via Blitz (PR #781)
)


# ---------------------------------------------------------------------------
# Domain normalization — canonical pattern, matches
# build_bridge_sam_pdl_domain_lance._normalize_domain_sql
# ---------------------------------------------------------------------------

_RE_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_RE_WWW = re.compile(r"^www\.", re.IGNORECASE)
_RE_PATH = re.compile(r"[/?#].*$")

# Government / state portals that occasionally leak through Overture as a
# business's "website" (e.g. an Alabama daycare whose Overture record lists
# dhr.state.al.us — the AL Dept of Human Resources, not the daycare). Drop
# these to avoid wasting Parallel.ai credit on guaranteed-wrong rows.
_RE_GOV_JUNK = re.compile(
    r"(?:\.gov|\.mil|\.state\.[a-z]{2}\.us)$",
    re.IGNORECASE,
)


def _is_gov_junk(domain: str) -> bool:
    return bool(_RE_GOV_JUNK.search(domain))


def normalize_domain(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _RE_SCHEME.sub("", s)
    s = _RE_WWW.sub("", s)
    s = _RE_PATH.sub("", s)
    s = s.strip()
    return s or None


# ---------------------------------------------------------------------------
# R2 / Lance helpers
# ---------------------------------------------------------------------------

def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _load_slow_lane() -> list[tuple[str, str]]:
    """Return (uei, normalized_domain) tuples from the slow-lane cohort.

    Applies the same ``_is_gov_junk`` filter the overture loader uses so
    we don't waste Parallel.ai credit on ``.gov`` / ``.mil`` /
    ``*.state.*.us`` domains that leak through SAM's ``corporate_website``
    or Overture's ``website_primary`` fallback. Some of these are
    legitimate (a city or sheriff's office *does* have a LinkedIn page)
    but the population is dominated by state-portal misattributions where
    a contractor's SAM record cites a state-agency URL instead of their
    own website — those are guaranteed-wrong.
    """
    import lance

    so = _r2_storage_options()
    ds = lance.dataset(COHORT_SLOW_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "loaded slow-lane cohort: %d rows in %dms (Lance version=%s)",
        tbl.num_rows, elapsed_ms, ds.version,
    )
    rows: list[tuple[str, str]] = []
    dropped_junk = 0
    for u, d in zip(tbl.column("uei").to_pylist(), tbl.column("domain").to_pylist()):
        if not u or not d:
            continue
        # Slow-lane domain was already normalized at emit time, but pass
        # through the same normalizer for idempotence + safety.
        nd = normalize_domain(d)
        if not nd:
            continue
        if _is_gov_junk(nd):
            dropped_junk += 1
            continue
        rows.append((u, nd))
    if dropped_junk:
        logger.info(
            "slow-lane: dropped %d rows for .gov/.mil/*.state.*.us",
            dropped_junk,
        )
    return rows


def _load_hop1_failures() -> list[tuple[str, str]]:
    """Return (uei, normalized_domain) tuples for hop1-retry UEIs.

    1. Pull failed UEIs from ``ops.task_runs`` (run_cmpnczfrk2u2j0umxfr6a3nvi).
    2. Look up ``corporate_website`` from ``spines/sam_entities_lance``,
       deduped one-row-per-UEI preferring non-null website.
    3. Normalize the website via the canonical pattern. Drop rows where
       normalization yields null/empty.
    """
    import lance
    import psycopg

    db_url = os.environ["HQX_DB_URL_POOLED"]
    t0 = time.perf_counter()
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT uei
                FROM ops.task_runs
                WHERE run_id = %s AND status = 'failed' AND uei IS NOT NULL
                """,
                (HOP1_FAILED_RUN_ID,),
            )
            failed_ueis = {row[0] for row in cur.fetchall()}
    logger.info(
        "hop1 failed UEIs: %d (run_id=%s) in %dms",
        len(failed_ueis), HOP1_FAILED_RUN_ID,
        int((time.perf_counter() - t0) * 1000),
    )
    if not failed_ueis:
        return []

    so = _r2_storage_options()
    ds = lance.dataset(SAM_ENTITIES_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "corporate_website"]).to_table()
    logger.info(
        "sam_entities scan: %d rows (pre-dedup), %dms",
        tbl.num_rows, int((time.perf_counter() - t0) * 1000),
    )

    # Dedupe one row per UEI, prefer non-null corporate_website.
    best: dict[str, str | None] = {}
    for u, w in zip(
        tbl.column("uei").to_pylist(),
        tbl.column("corporate_website").to_pylist(),
    ):
        if not u or u not in failed_ueis:
            continue
        if u not in best or (best[u] is None and w):
            best[u] = w

    rows: list[tuple[str, str]] = []
    dropped_junk = 0
    for u, w in best.items():
        nd = normalize_domain(w)
        if not nd:
            continue
        # Same .gov/.mil/*.state.*.us filter as slow_lane + overture loaders.
        # corporate_website is sourced from SAM, where contractors sometimes
        # cite a state agency portal instead of their own site.
        if _is_gov_junk(nd):
            dropped_junk += 1
            continue
        rows.append((u, nd))
    if dropped_junk:
        logger.info(
            "hop1_retry: dropped %d rows for .gov/.mil/*.state.*.us",
            dropped_junk,
        )
    logger.info(
        "hop1 retries after SAM lookup + normalization: %d (dropped %d for "
        "missing/empty normalized domain)",
        len(rows), len(failed_ueis) - len(rows),
    )
    return rows


def _load_overture_cohort() -> list[tuple[str, str]]:
    """Return (uei, normalized_domain) tuples from the Overture cohort.

    Cohort schema is a strict superset of ``cohorts/primes_90d_slow`` —
    columns ``uei`` and ``domain`` are first-class and already normalized
    at emit time. We re-normalize here for idempotence and apply the
    ``.gov`` / ``.mil`` / ``*.state.*.us`` junk filter in-script so the
    cohort itself does not need to be re-emitted to strip the long-tail
    leakage from Overture's "website" column.
    """
    import lance

    so = _r2_storage_options()
    ds = lance.dataset(COHORT_OVERTURE_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "loaded overture cohort: %d rows in %dms (Lance version=%s)",
        tbl.num_rows, elapsed_ms, ds.version,
    )
    rows: list[tuple[str, str]] = []
    dropped_junk = 0
    dropped_empty = 0
    for u, d in zip(tbl.column("uei").to_pylist(), tbl.column("domain").to_pylist()):
        if not u or not d:
            dropped_empty += 1
            continue
        nd = normalize_domain(d)
        if not nd:
            dropped_empty += 1
            continue
        if _is_gov_junk(nd):
            dropped_junk += 1
            continue
        rows.append((u, nd))
    logger.info(
        "overture cohort after filters: %d (dropped %d gov/mil/state-portal, %d empty/null)",
        len(rows), dropped_junk, dropped_empty,
    )
    return rows


def _load_fmcsa_sam_no_pdl_cohort() -> list[tuple[str, str]]:
    """Return (uei, normalized_domain) tuples from the FMCSA × SAM no-PDL cohort.

    Cohort grain is one row per UEI (the emit script collapses fan-out by
    MIN(dot_number)) and the ``domain`` column is already normalized +
    junk-filtered, so this loader is a straight Lance scan + idempotence
    pass through ``normalize_domain``.
    """
    import lance

    so = _r2_storage_options()
    ds = lance.dataset(COHORT_FMCSA_SAM_NO_PDL_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei", "domain"]).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "loaded fmcsa_sam_no_pdl cohort: %d rows in %dms (Lance version=%s)",
        tbl.num_rows, elapsed_ms, ds.version,
    )
    rows: list[tuple[str, str]] = []
    dropped = 0
    for u, d in zip(tbl.column("uei").to_pylist(), tbl.column("domain").to_pylist()):
        if not u or not d:
            dropped += 1
            continue
        nd = normalize_domain(d)
        if not nd:
            dropped += 1
            continue
        rows.append((u, nd))
    if dropped:
        logger.info(
            "fmcsa_sam_no_pdl cohort: dropped %d rows for null/empty after re-normalization",
            dropped,
        )
    return rows


def _load_sam_active_no_pdl_midtier_cohort() -> list[tuple[str, str]]:
    """Return (uei, normalized_domain) tuples from the SAM active no-PDL midtier cohort.

    The cohort schema carries the canonical ``entity_url_normalized`` column
    (33.7% present per the build script) and ``total_365d`` USD obligation.
    Rows are returned **sorted by ``total_365d`` desc** so a downstream
    ``--limit N`` enriches the highest-revenue UEIs first; critical when
    running under a Parallel.ai credit cap.

    Rows with null/empty ``entity_url_normalized`` are dropped — they have
    no domain to submit to the orchestrator's domain→linkedin task.
    """
    import lance
    import pyarrow.compute as pc

    so = _r2_storage_options()
    ds = lance.dataset(COHORT_SAM_ACTIVE_NO_PDL_MIDTIER_URI, storage_options=so)
    t0 = time.perf_counter()
    # Project + filter at scan time: only rows with a non-null normalized URL.
    tbl = ds.scanner(
        columns=["uei", "entity_url_normalized", "total_365d"],
        filter=pc.field("entity_url_normalized").is_valid(),
    ).to_table()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "loaded sam_active_no_pdl_midtier cohort: %d rows (with entity_url_normalized) "
        "in %dms (Lance version=%s)",
        tbl.num_rows, elapsed_ms, ds.version,
    )

    # Sort by total_365d desc so callers' --limit N picks the top-revenue UEIs.
    sort_indices = pc.sort_indices(
        tbl, sort_keys=[("total_365d", "descending")],
    )
    tbl = tbl.take(sort_indices)

    rows: list[tuple[str, str]] = []
    dropped = 0
    for u, d in zip(
        tbl.column("uei").to_pylist(),
        tbl.column("entity_url_normalized").to_pylist(),
    ):
        if not u or not d:
            dropped += 1
            continue
        nd = normalize_domain(d)
        if not nd:
            dropped += 1
            continue
        rows.append((u, nd))
    if dropped:
        logger.info(
            "sam_active_no_pdl_midtier: dropped %d rows for null/empty after re-normalization",
            dropped,
        )
    return rows


def _load_already_enriched_ueis() -> set[str]:
    """UEIs that already have a domain → linkedin attempt of any provider.

    Anti-join target spans every task_type in
    ``LEDGER_DOMAIN_TO_LINKEDIN_TASK_TYPES`` — Parallel.ai's own rows
    plus sibling providers (Clay backfill, Trigger.dev/Blitz Hop 1) so
    this orchestrator never double-spends on UEIs another path already
    handled. Status is intentionally unfiltered: every UEI that's been
    through ANY provider (completed / not_found / failed) is excluded.
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
                WHERE task_type = ANY(%s)
                  AND uei IS NOT NULL
                """,
                (list(LEDGER_DOMAIN_TO_LINKEDIN_TASK_TYPES),),
            )
            ueis = {row[0] for row in cur.fetchall()}
    logger.info(
        "already-enriched UEIs (task_types=%s): %d in %dms",
        ",".join(LEDGER_DOMAIN_TO_LINKEDIN_TASK_TYPES),
        len(ueis), int((time.perf_counter() - t0) * 1000),
    )
    return ueis


def _build_input_set(
    limit: int | None,
    sources: tuple[str, ...],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Return (input_rows, source_by_uei) honoring source precedence and the
    ``parallel_domain_to_linkedin`` anti-join for idempotence.

    Precedence (when a UEI appears in multiple populations):
        slow_lane > hop1_retry > overture_websites_sam_no_url
            > fmcsa_sam_no_pdl > sam_active_no_pdl_midtier
    """
    slow = _load_slow_lane() if SOURCE_SLOW_LANE in sources else []
    hop1 = _load_hop1_failures() if SOURCE_HOP1_RETRY in sources else []
    overture = _load_overture_cohort() if SOURCE_OVERTURE in sources else []
    fmcsa_no_pdl = (
        _load_fmcsa_sam_no_pdl_cohort() if SOURCE_FMCSA_SAM_NO_PDL in sources else []
    )
    sam_active = (
        _load_sam_active_no_pdl_midtier_cohort()
        if SOURCE_SAM_ACTIVE_NO_PDL_MIDTIER in sources else []
    )

    source_by_uei: dict[str, str] = {}
    domain_by_uei: dict[str, str] = {}
    for u, d in slow:
        if u not in domain_by_uei:
            domain_by_uei[u] = d
            source_by_uei[u] = SOURCE_SLOW_LANE
    overlap_hop1 = 0
    for u, d in hop1:
        if u in domain_by_uei:
            overlap_hop1 += 1
            continue
        domain_by_uei[u] = d
        source_by_uei[u] = SOURCE_HOP1_RETRY
    overlap_overture = 0
    for u, d in overture:
        if u in domain_by_uei:
            overlap_overture += 1
            continue
        domain_by_uei[u] = d
        source_by_uei[u] = SOURCE_OVERTURE
    overlap_fmcsa = 0
    for u, d in fmcsa_no_pdl:
        if u in domain_by_uei:
            overlap_fmcsa += 1
            continue
        domain_by_uei[u] = d
        source_by_uei[u] = SOURCE_FMCSA_SAM_NO_PDL
    overlap_sam_active = 0
    for u, d in sam_active:
        if u in domain_by_uei:
            overlap_sam_active += 1
            continue
        domain_by_uei[u] = d
        source_by_uei[u] = SOURCE_SAM_ACTIVE_NO_PDL_MIDTIER
    logger.info(
        "union: slow=%d hop1=%d overture=%d fmcsa_no_pdl=%d sam_active=%d "
        "overlap_hop1_in_slow=%d overlap_overture_in_prior=%d "
        "overlap_fmcsa_in_prior=%d overlap_sam_active_in_prior=%d "
        "unique_total=%d",
        len(slow), len(hop1), len(overture), len(fmcsa_no_pdl), len(sam_active),
        overlap_hop1, overlap_overture, overlap_fmcsa, overlap_sam_active,
        len(domain_by_uei),
    )

    already = _load_already_enriched_ueis()
    excluded = 0
    pruned_domain_by_uei: dict[str, str] = {}
    for u, d in domain_by_uei.items():
        if u in already:
            excluded += 1
            continue
        pruned_domain_by_uei[u] = d
    logger.info(
        "anti-join vs existing %s ledger: excluded=%d remaining=%d",
        TASK_TYPE, excluded, len(pruned_domain_by_uei),
    )
    domain_by_uei = pruned_domain_by_uei
    # Drop source tags for excluded UEIs so the per-row source attribution is
    # consistent with the actual submission set.
    source_by_uei = {u: source_by_uei[u] for u in domain_by_uei}

    input_rows = [{"uei": u, "domain": domain_by_uei[u]} for u in domain_by_uei]
    if limit is not None:
        input_rows = input_rows[:limit]
        logger.info("--limit %d → truncated input set to %d rows", limit, len(input_rows))
    return input_rows, source_by_uei


# ---------------------------------------------------------------------------
# Parallel.ai submission
# ---------------------------------------------------------------------------

def _build_task_spec():
    from parallel.types import JsonSchemaParam, TaskSpecParam
    from parallel.types.run_input_param import RunInputParam  # noqa: F401  (imported by caller)

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "uei": {
                "type": "string",
                "description": "SAM.gov Unique Entity Identifier (12-char alphanumeric).",
            },
            "domain": {
                "type": "string",
                "description": (
                    "Normalized website domain for the company "
                    "(lowercase, no scheme, no www., no path)."
                ),
            },
        },
        "required": ["uei", "domain"],
    }
    output_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "company_linkedin_url": {
                "type": "string",
                "description": "The company_linkedin_url for a given domain",
            },
        },
        "required": [],
    }
    return TaskSpecParam(
        input_schema=JsonSchemaParam(type="json", json_schema=input_schema),
        output_schema=JsonSchemaParam(type="json", json_schema=output_schema),
    )


def _submit_group(client, task_spec, input_rows: list[dict[str, str]]) -> str:
    from parallel.types.run_input_param import RunInputParam

    group = client.task_group.create()
    group_id = group.task_group_id
    logger.info("created task_group %s", group_id)

    total_added = 0
    chunks = [
        input_rows[i : i + PARALLEL_ADD_RUNS_CHUNK]
        for i in range(0, len(input_rows), PARALLEL_ADD_RUNS_CHUNK)
    ]
    for idx, chunk in enumerate(chunks, start=1):
        runs = [
            RunInputParam(input=row, processor=PARALLEL_PROCESSOR) for row in chunk
        ]
        resp = client.task_group.add_runs(
            group_id, inputs=runs, default_task_spec=task_spec,
        )
        added = len(resp.run_ids)
        total_added += added
        logger.info(
            "add_runs chunk %d/%d: submitted=%d (cumulative=%d)",
            idx, len(chunks), added, total_added,
        )
    if total_added != len(input_rows):
        logger.warning(
            "add_runs total=%d != input_rows=%d (Parallel deduped or rejected)",
            total_added, len(input_rows),
        )
    return group_id


def _await_group(client, group_id: str) -> None:
    """Poll until the group reports not-active. Hard cap PARALLEL_POLL_TIMEOUT_S."""
    started = time.time()
    last_log_t = 0.0
    while True:
        if time.time() - started > PARALLEL_POLL_TIMEOUT_S:
            raise RuntimeError(
                f"Parallel.ai group {group_id} did not complete within "
                f"{PARALLEL_POLL_TIMEOUT_S}s",
            )
        tg = client.task_group.retrieve(group_id)
        status = tg.status
        # status fields documented: task_run_status_counts (dict-like), is_active
        if time.time() - last_log_t > 30:
            counts = getattr(status, "task_run_status_counts", None)
            logger.info(
                "poll group=%s active=%s counts=%s (elapsed=%ds)",
                group_id, status.is_active, counts, int(time.time() - started),
            )
            last_log_t = time.time()
        if not status.is_active:
            logger.info(
                "group %s reached terminal state after %ds",
                group_id, int(time.time() - started),
            )
            return
        time.sleep(PARALLEL_POLL_INTERVAL_S)


def _collect_results(client, group_id: str) -> list[dict[str, Any]]:
    """Stream get_runs() and project per-row results."""
    from parallel.types.task_run_event import TaskRunEvent
    from parallel.types.error_event import ErrorEvent

    out: list[dict[str, Any]] = []
    event_count = 0
    stream = client.task_group.get_runs(
        group_id, include_input=True, include_output=True,
    )
    for event in stream:
        event_count += 1
        if isinstance(event, ErrorEvent):
            out.append({"kind": "error", "raw": _safe_dump(event)})
            continue
        if not isinstance(event, TaskRunEvent):
            continue  # ignore unknown event types
        run = event.run
        run_id = getattr(run, "run_id", None)
        run_status = getattr(run, "status", None)
        # original input (when include_input=True)
        original_input: dict[str, Any] | None = None
        if event.input is not None:
            inp = getattr(event.input, "input", None)
            if isinstance(inp, dict):
                original_input = inp
            elif isinstance(inp, str):
                try:
                    original_input = json.loads(inp)
                except Exception:
                    original_input = None
        # output (only on success)
        content: Any = None
        basis: Any = None
        if event.output is not None:
            content = getattr(event.output, "content", None)
            basis = getattr(event.output, "basis", None)
        out.append(
            {
                "kind": "run",
                "run_id": run_id,
                "status": run_status,
                "input": original_input,
                "content": content,
                "basis": basis,
            },
        )
    logger.info("collected %d events from group %s", event_count, group_id)
    return out


def _safe_dump(obj: Any) -> Any:
    try:
        return obj.model_dump()  # pydantic v2
    except Exception:
        pass
    try:
        return dict(obj)
    except Exception:
        return repr(obj)


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------

_LINKEDIN_RE = re.compile(r"linkedin\.com", re.IGNORECASE)


def _classify(content: Any) -> tuple[str, str | None]:
    """Return (status, linkedin_url|None) for a successful Parallel run."""
    if not isinstance(content, dict):
        return ("not_found", None)
    raw = content.get("company_linkedin_url")
    if not isinstance(raw, str):
        return ("not_found", None)
    s = raw.strip()
    if not s:
        return ("not_found", None)
    if not _LINKEDIN_RE.search(s):
        return ("not_found", None)
    return ("completed", s)


def _write_ledger(
    *,
    run_id: str,
    results: list[dict[str, Any]],
    source_by_uei: dict[str, str],
) -> dict[str, int]:
    """Insert per-UEI rows into ops.task_runs. Returns status counts."""
    import psycopg

    db_url = os.environ["HQX_DB_URL_POOLED"]
    counts = {"completed": 0, "not_found": 0, "failed": 0, "skipped": 0}
    rows_to_insert: list[tuple[str, str, str, str, str | None, str | None, str]] = []

    for r in results:
        if r.get("kind") != "run":
            counts["skipped"] += 1
            continue
        original_input = r.get("input") or {}
        uei = original_input.get("uei") if isinstance(original_input, dict) else None
        domain = original_input.get("domain") if isinstance(original_input, dict) else None
        if not uei:
            counts["skipped"] += 1
            continue
        run_status_remote = r.get("status")
        content = r.get("content")
        basis = r.get("basis")
        source = source_by_uei.get(uei, "unknown")

        if content is None:
            # Parallel run did not produce output → failed.
            status = "failed"
            linkedin_url = None
            payload = {
                "company_linkedin_url": None,
                "basis": None,
                "source": source,
                "parallel_run_id": r.get("run_id"),
                "parallel_run_status": run_status_remote,
            }
        else:
            status, linkedin_url = _classify(content)
            payload = {
                "company_linkedin_url": linkedin_url,
                "basis": _jsonable(basis),
                "source": source,
                "parallel_run_id": r.get("run_id"),
            }
        counts[status] = counts.get(status, 0) + 1
        rows_to_insert.append(
            (run_id, uei, TASK_TYPE, status, domain, linkedin_url, json.dumps(payload)),
        )

    if not rows_to_insert:
        logger.warning("no rows to insert into ops.task_runs")
        return counts

    t0 = time.perf_counter()
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ops.task_runs
                    (run_id, uei, task_type, status, domain, linkedin_url,
                     result_payload, inputs_count, outputs_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 1,
                        CASE WHEN %s = 'completed' THEN 1 ELSE 0 END)
                ON CONFLICT (run_id, uei) DO NOTHING
                """,
                [
                    (run_id, uei, task_type, status, domain, linkedin_url,
                     payload, status)
                    for (run_id, uei, task_type, status, domain, linkedin_url,
                         payload) in rows_to_insert
                ],
            )
        conn.commit()
    logger.info(
        "wrote %d ledger rows in %dms (run_id=%s)",
        len(rows_to_insert),
        int((time.perf_counter() - t0) * 1000),
        run_id,
    )
    return counts


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON-safe coercion for Parallel basis objects."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    # pydantic v2
    try:
        return obj.model_dump(mode="json")
    except Exception:
        pass
    try:
        return obj.dict()
    except Exception:
        pass
    return repr(obj)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="Submit to Parallel.ai + write ledger.")
    grp.add_argument("--dry-run", action="store_true",
                     help="Build the input set + print counts; no API calls.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap input rows (smoke test).")
    ap.add_argument(
        "--source",
        choices=(
            "all",
            SOURCE_SLOW_LANE,
            SOURCE_HOP1_RETRY,
            SOURCE_OVERTURE,
            SOURCE_FMCSA_SAM_NO_PDL,
            SOURCE_SAM_ACTIVE_NO_PDL_MIDTIER,
        ),
        default="all",
        help=(
            "Which source population(s) to enrich. Default 'all' unions "
            "slow_lane + hop1_retry + overture_websites_sam_no_url with "
            "precedence in that order. Restrict to a single source to "
            "avoid re-spending credit when extending an already-enriched "
            "population (the anti-join handles overlap, but this saves the "
            "Lance scans entirely)."
        ),
    )
    args = ap.parse_args()

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "HQX_DB_URL_POOLED",
    ):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("PARALLEL_API_KEY"):
        raise SystemExit("FAIL: PARALLEL_API_KEY not set")

    sources: tuple[str, ...] = ALL_SOURCES if args.source == "all" else (args.source,)
    logger.info("source selection: %s", ", ".join(sources))

    input_rows, source_by_uei = _build_input_set(args.limit, sources)
    if not input_rows:
        logger.warning("nothing to submit — exiting clean")
        return 0

    if args.dry_run:
        sample = input_rows[: min(5, len(input_rows))]
        logger.info("dry-run sample (first %d):", len(sample))
        for r in sample:
            logger.info(
                "  uei=%s domain=%s source=%s",
                r["uei"], r["domain"], source_by_uei.get(r["uei"], "?"),
            )
        per_source: dict[str, int] = {}
        for u in (r["uei"] for r in input_rows):
            s = source_by_uei.get(u, "unknown")
            per_source[s] = per_source.get(s, 0) + 1
        print(f"DRY_RUN_TOTAL: {len(input_rows)}")
        for s in ALL_SOURCES:
            print(f"DRY_RUN_{s.upper()}: {per_source.get(s, 0)}")
        return 0

    from parallel import Parallel

    run_id = uuid.uuid4().hex
    logger.info("orchestrator run_id=%s rows=%d", run_id, len(input_rows))

    client = Parallel(api_key=os.environ["PARALLEL_API_KEY"])
    task_spec = _build_task_spec()
    group_id = _submit_group(client, task_spec, input_rows)
    _await_group(client, group_id)
    results = _collect_results(client, group_id)
    counts = _write_ledger(
        run_id=run_id, results=results, source_by_uei=source_by_uei,
    )

    print(f"PARALLEL_GROUP_ID: {group_id}")
    print(f"LEDGER_RUN_ID: {run_id}")
    print(f"TOTAL_SUBMITTED: {len(input_rows)}")
    print(f"COMPLETED: {counts.get('completed', 0)}")
    print(f"NOT_FOUND: {counts.get('not_found', 0)}")
    print(f"FAILED: {counts.get('failed', 0)}")
    print(f"SKIPPED: {counts.get('skipped', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
