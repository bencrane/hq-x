"""Emit 365d-winner GTM hydration cohorts (fast / hop1) → existing Lane Lance.

Unlike ``build_cohort_primes_90d_lance.py`` (90-day FPDS window, gated emit
with multiple domain-source fallbacks), this script materializes the EXACT
365-day-winner sets the operator scoped, into the SAME cohort datasets the
existing Trigger tasks already read — so no task/deploy change is needed:

  --lane fast  → 365d winners WITH a resolved company_linkedin_url, NOT yet
                 Hop-2 firmo-terminal. Written to ``cohorts/primes_90d_fast``
                 (uei, domain, linkedin_url, linkedin_source) so the existing
                 ``gtm_hydration_90d_fast`` Trigger task hydrates them unchanged
                 (single Blitz ``/v2/enrichment/company`` call per row).

  --lane hop1  → 365d winners WITH a website but NO LinkedIn URL, NOT yet
                 Hop-2 terminal. Written to ``cohorts/primes_90d_slow``
                 (uei, domain) so the existing ``gtm_hydration_90d_slow``
                 Trigger task runs the Blitz two-hop (domain→linkedin→firmo).

"365d winner" = any UEI with a non-null obligation event in
``usaspending/transaction_fpds_lance`` (``recipient_uei``) or
``usaspending/subaward_lance`` (``sub_awardee_or_recipient_uei``) within the
trailing 365 days. Threshold $0 — mirrors ``MIN_OBLIGATION_90D_USD = 0`` in the
sibling 90d emit (the gate is "appeared in the window," not a dollar floor).

LinkedIn URL universe (fast lane key):
  * ``bridges/sam_pdl_lance.pdl_linkedin_url``        → source='pdl'
  * ``ops.task_runs`` completed Hop-1 rows            → source∈{parallel,clay,trigger_blitz}
    (task_type ∈ parallel_/clay_/trigger_blitz_domain_to_linkedin)

Website universe (hop1 lane key — no LinkedIn yet):
  * ``spines/sam_entities_lance.corporate_website``
  * ``cohorts/overture_websites_sam_no_url_lance.domain``
  * ``cohorts/fmcsa_sam_no_pdl_lance.domain``

Anti-join (both lanes): excludes UEIs already terminal in ``ops.task_runs``
for the firmographic task_types ``modal_hydrate_firmo_cascade`` /
``blitz_firmo_direct`` (status ∈ completed/not_found/failed). Pending rows are
NOT excluded — they get retried, matching the 90d emit's contract.

Rows are sorted by trailing-365d obligation DESC so ``--limit N`` processes the
highest-revenue UEIs first. Re-runnable / idempotent: each emit re-derives a
fresh anti-join, so a cycle orchestrator loops emit→trigger→wait until the
cohort drains.

Domains are normalized (lowercase, no scheme/www/path) and ``.gov``/``.mil``/
``*.state.*.us`` junk is dropped — same convention as the Parallel.ai loader.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/build_cohort_won_365d_lance.py --lane fast --dry-run
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/build_cohort_won_365d_lance.py --lane fast --limit 6000
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/build_cohort_won_365d_lance.py --lane hop1
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from pathlib import Path

# Project-relative imports — reuse the 90d emit's Lance write / Polaris
# register / storage-options plumbing so write conventions (BTREE on uei,
# overwrite mode, compaction, Polaris registration) stay identical.
_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
_DEX_ROOT = _SCRIPTS_DIR.parent
if str(_DEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEX_ROOT))

from scripts.build_cohort_primes_90d_lance import (  # noqa: E402
    COHORT_FAST_SLUG,
    COHORT_FAST_URI,
    COHORT_SLOW_SLUG,
    COHORT_SLOW_URI,
    _r2_storage_options,
    _register_polaris,
    _write_lance,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_cohort_won_365d")

WINDOW_DAYS = 365

FPDS_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fpds_lance"
SUBAWARD_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance"
SAM_PDL_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"
SAM_ENTITIES_URI = "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance"
OVERTURE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/overture_websites_sam_no_url_lance"
FMCSA_SAM_URI = "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/fmcsa_sam_no_pdl_lance"

# ── Domain normalization (matches run_parallel_domain_to_linkedin.py) ────────
_RE_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_RE_WWW = re.compile(r"^www\.", re.IGNORECASE)
_RE_PATH = re.compile(r"[/?#].*$")
_RE_GOV_JUNK = re.compile(r"(?:\.gov|\.mil|\.state\.[a-z]{2}\.us)$", re.IGNORECASE)


def _is_gov_junk(domain: str) -> bool:
    return bool(_RE_GOV_JUNK.search(domain))


def normalize_domain(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _RE_SCHEME.sub("", s)
    s = _RE_WWW.sub("", s)
    s = _RE_PATH.sub("", s)
    s = s.strip()
    if not s or _is_gov_junk(s):
        return None
    return s


# ── Scans ────────────────────────────────────────────────────────────────────

def _window_lo() -> str:
    return (dt.date.today() - dt.timedelta(days=WINDOW_DAYS)).isoformat()


def _scan_winner_obligations(so: dict[str, str]) -> dict[str, float]:
    """Return {uei: trailing-365d obligation USD} across FPDS prime + subaward.

    Threshold $0 — any non-null obligation event in the window qualifies the
    UEI as a "winner." The summed amount is used only for ORDER BY so --limit
    picks the highest-revenue UEIs first.
    """
    import lance
    import pyarrow.compute as pc

    lo = _window_lo()
    logger.info("winner window: action_date >= %s", lo)
    obligations: dict[str, float] = {}

    fpds = lance.dataset(FPDS_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = fpds.scanner(
        columns=["recipient_uei", "federal_action_obligation"],
        filter=(pc.field("action_date") >= lo),
    ).to_table()
    for u, amt in zip(
        tbl.column("recipient_uei").to_pylist(),
        tbl.column("federal_action_obligation").to_pylist(),
    ):
        if not u:
            continue
        try:
            v = float(amt) if amt is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        obligations[u] = obligations.get(u, 0.0) + v
    logger.info(
        "fpds prime winners: %d UEIs in %dms",
        len(obligations), int((time.perf_counter() - t0) * 1000),
    )

    sub = lance.dataset(SUBAWARD_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = sub.scanner(
        columns=["sub_awardee_or_recipient_uei", "subaward_amount"],
        filter=(pc.field("sub_action_date") >= lo),
    ).to_table()
    before = len(obligations)
    for u, amt in zip(
        tbl.column("sub_awardee_or_recipient_uei").to_pylist(),
        tbl.column("subaward_amount").to_pylist(),
    ):
        if not u:
            continue
        try:
            v = float(amt) if amt is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        obligations[u] = obligations.get(u, 0.0) + v
    logger.info(
        "+ subaward winners: +%d new UEIs (total %d) in %dms",
        len(obligations) - before, len(obligations),
        int((time.perf_counter() - t0) * 1000),
    )
    return obligations


def _scan_pdl_linkedins(so: dict[str, str]) -> dict[str, str]:
    """{uei: pdl_linkedin_url} for UEIs PDL resolved a company LinkedIn URL."""
    import lance
    import pyarrow.compute as pc

    ds = lance.dataset(SAM_PDL_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=["uei", "pdl_linkedin_url"],
        filter=pc.field("pdl_linkedin_url").is_valid(),
    ).to_table()
    out: dict[str, str] = {}
    for u, url in zip(
        tbl.column("uei").to_pylist(), tbl.column("pdl_linkedin_url").to_pylist()
    ):
        if u and url and u not in out:
            out[u] = url
    logger.info(
        "pdl linkedins: %d UEIs in %dms",
        len(out), int((time.perf_counter() - t0) * 1000),
    )
    return out


def _scan_sam_websites(so: dict[str, str]) -> dict[str, str]:
    """{uei: normalized_domain} from SAM corporate_website (deduped per UEI)."""
    import lance
    import pyarrow.compute as pc

    ds = lance.dataset(SAM_ENTITIES_URI, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=["uei", "corporate_website"],
        filter=pc.field("corporate_website").is_valid(),
    ).to_table()
    out: dict[str, str] = {}
    for u, w in zip(
        tbl.column("uei").to_pylist(), tbl.column("corporate_website").to_pylist()
    ):
        if not u or u in out:
            continue
        nd = normalize_domain(w)
        if nd:
            out[u] = nd
    logger.info(
        "sam corporate_website: %d UEIs (normalized, junk-filtered) in %dms",
        len(out), int((time.perf_counter() - t0) * 1000),
    )
    return out


def _scan_cohort_domains(so: dict[str, str], uri: str, label: str) -> dict[str, str]:
    """{uei: normalized_domain} from a cohort Lance with (uei, domain)."""
    import lance
    import pyarrow.compute as pc

    ds = lance.dataset(uri, storage_options=so)
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=["uei", "domain"], filter=pc.field("domain").is_valid()
    ).to_table()
    out: dict[str, str] = {}
    for u, d in zip(tbl.column("uei").to_pylist(), tbl.column("domain").to_pylist()):
        if not u or u in out:
            continue
        nd = normalize_domain(d)
        if nd:
            out[u] = nd
    logger.info(
        "%s: %d UEIs (normalized, junk-filtered) in %dms",
        label, len(out), int((time.perf_counter() - t0) * 1000),
    )
    return out


def _fetch_ledger_sets() -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Return (sideloader_resolved, hop2_terminal).

    sideloader_resolved: {uei: (domain, source)} for completed Hop-1 rows,
        precedence parallel > clay > trigger_blitz.
    hop2_terminal: UEIs already terminal for firmographic enrichment.
    """
    import psycopg

    db_url = os.environ["HQX_DB_URL_POOLED"]
    sideloader: dict[str, tuple[str, str]] = {}
    rank = {"parallel": 1, "clay": 2, "trigger_blitz": 3}
    best_rank: dict[str, int] = {}
    t0 = time.perf_counter()
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uei, domain,
                       CASE task_type
                           WHEN 'parallel_domain_to_linkedin'      THEN 'parallel'
                           WHEN 'clay_domain_to_linkedin'          THEN 'clay'
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
                """
            )
            for uei, domain, source in cur.fetchall():
                r = rank.get(source, 9)
                if uei not in best_rank or r < best_rank[uei]:
                    best_rank[uei] = r
                    sideloader[uei] = (domain, source)
            cur.execute(
                """
                SELECT DISTINCT uei
                FROM ops.task_runs
                WHERE task_type IN ('modal_hydrate_firmo_cascade', 'blitz_firmo_direct')
                  AND status IN ('completed', 'not_found', 'failed')
                  AND uei IS NOT NULL
                """
            )
            hop2_terminal = {r[0] for r in cur.fetchall()}
    logger.info(
        "ledger: sideloader_resolved=%d hop2_terminal=%d in %dms",
        len(sideloader), len(hop2_terminal), int((time.perf_counter() - t0) * 1000),
    )
    return sideloader, hop2_terminal


# ── Set assembly ──────────────────────────────────────────────────────────────

def _build_fast_rows(so, winners, obligations, pdl, sideloader, hop2_terminal):
    """fast lane: winners WITH a LinkedIn URL, not Hop-2 terminal.

    LinkedIn = PDL ∪ sideloader, precedence PDL > sideloader (no double-count).
    Rows: (uei, domain, linkedin_url, linkedin_source), sorted by 365d $ desc.
    """
    sam_sites = _scan_sam_websites(so)
    rows: list[tuple[str, str | None, str, str]] = []
    # PDL-resolved winners (domain is informational — Modal skips Hop1 when URL set).
    for uei in winners:
        if uei in hop2_terminal or uei not in pdl:
            continue
        rows.append((uei, sam_sites.get(uei), pdl[uei], "pdl"))
    # Sideloader-resolved winners not already covered by PDL.
    _attach_sideloader_urls(rows, winners, hop2_terminal, pdl, sideloader)
    rows.sort(key=lambda r: obligations.get(r[0], 0.0), reverse=True)
    return rows


def _attach_sideloader_urls(rows, winners, hop2_terminal, pdl, sideloader):
    """Append sideloader-resolved fast rows (uei not already covered by PDL).

    The sideloader dict only stores (domain, source); we need the linkedin_url
    itself, pulled here from ops.task_runs in one pass with provider precedence.
    """
    import psycopg

    need = [
        u for u in winners
        if u not in hop2_terminal and u not in pdl and u in sideloader
    ]
    if not need:
        return
    db_url = os.environ["HQX_DB_URL_POOLED"]
    urls: dict[str, str] = {}
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (uei) uei, linkedin_url
                FROM ops.task_runs
                WHERE task_type IN (
                          'parallel_domain_to_linkedin',
                          'clay_domain_to_linkedin',
                          'trigger_blitz_domain_to_linkedin'
                      )
                  AND status = 'completed'
                  AND linkedin_url IS NOT NULL
                  AND uei = ANY(%s)
                ORDER BY uei,
                         CASE task_type
                             WHEN 'parallel_domain_to_linkedin'      THEN 1
                             WHEN 'clay_domain_to_linkedin'          THEN 2
                             WHEN 'trigger_blitz_domain_to_linkedin' THEN 3
                         END
                """,
                (need,),
            )
            for uei, url in cur.fetchall():
                urls[uei] = url
    for uei in need:
        url = urls.get(uei)
        if not url:
            continue
        domain, source = sideloader[uei]
        rows.append((uei, normalize_domain(domain), url, source))


def _build_hop1_rows(so, winners, obligations, pdl, sideloader, hop2_terminal):
    """hop1 lane: winners WITH a website but NO LinkedIn URL, not Hop-2 terminal.

    Rows: (uei, domain), sorted by 365d $ desc.
    """
    sam_sites = _scan_sam_websites(so)
    overture = _scan_cohort_domains(so, OVERTURE_URI, "overture")
    fmcsa = _scan_cohort_domains(so, FMCSA_SAM_URI, "fmcsa_sam_no_pdl")

    rows: list[tuple[str, str]] = []
    for uei in winners:
        if uei in hop2_terminal or uei in pdl or uei in sideloader:
            continue
        domain = sam_sites.get(uei) or overture.get(uei) or fmcsa.get(uei)
        if not domain:
            continue
        rows.append((uei, domain))
    rows.sort(key=lambda r: obligations.get(r[0], 0.0), reverse=True)
    return rows


# ── Write ──────────────────────────────────────────────────────────────────

def _write_rows(rows: list[tuple], columns: list[str], slug: str, uri: str, so) -> int:
    import duckdb
    import pyarrow as pa

    arrays = {col: pa.array([r[i] for r in rows], type=pa.string())
              for i, col in enumerate(columns)}
    tbl = pa.table(arrays)
    con = duckdb.connect()
    con.register("final", tbl)
    rel = con.from_query("SELECT * FROM final")
    return _write_lance(rel, slug=slug, uri=uri, storage_options=so)


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=("fast", "hop1"), required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap rows at N (highest 365d obligation first).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute + log counts; do NOT write Lance / Polaris.")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "HQX_DB_URL_POOLED"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    so = _r2_storage_options()
    obligations = _scan_winner_obligations(so)
    winners = set(obligations.keys())
    pdl = _scan_pdl_linkedins(so)
    sideloader, hop2_terminal = _fetch_ledger_sets()

    if args.lane == "fast":
        rows = _build_fast_rows(so, winners, obligations, pdl, sideloader, hop2_terminal)
        columns = ["uei", "domain", "linkedin_url", "linkedin_source"]
        slug, uri = COHORT_FAST_SLUG, COHORT_FAST_URI
        docstring = (
            "GTM 365d-winner cohort — fast lane (LinkedIn-URL resolved; "
            "PDL + sideloader). Emitted by build_cohort_won_365d_lance.py."
        )
    else:
        rows = _build_hop1_rows(so, winners, obligations, pdl, sideloader, hop2_terminal)
        columns = ["uei", "domain"]
        slug, uri = COHORT_SLOW_SLUG, COHORT_SLOW_URI
        docstring = (
            "GTM 365d-winner cohort — hop1/slow lane (website, no LinkedIn URL). "
            "Emitted by build_cohort_won_365d_lance.py."
        )

    total = len(rows)
    logger.info("lane=%s eligible rows (pre-limit): %d", args.lane, total)

    if args.limit is not None and args.limit < total:
        rows = rows[: args.limit]
        logger.info("--limit %d → truncated to %d rows (top by 365d $)", args.limit, len(rows))

    if args.dry_run:
        print(f"LANE: {args.lane}")
        print(f"ELIGIBLE_TOTAL: {total}")
        print(f"WOULD_WRITE: {len(rows)}")
        for r in rows[:5]:
            print(f"  sample: {r}")
        return 0

    if not rows:
        logger.warning("lane=%s — 0 rows; writing empty cohort (drains the lane)", args.lane)

    written = _write_rows(rows, columns, slug, uri, so)
    _register_polaris(slug, uri, docstring)
    print(f"LANE: {args.lane}")
    print(f"ELIGIBLE_TOTAL: {total}")
    print(f"COHORT_ROW_COUNT: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
