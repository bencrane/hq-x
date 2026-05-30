#!/usr/bin/env python3
"""FINRA BrokerCheck firms ingest from the public JSON API at api.brokercheck.finra.org.

Source-first per CLAUDE.md (2026-04-16): firms-only first pass; no merge into
target_companies, no identity resolution to existing RIA / IAPD ingest.

Two phases:
  phase1 — recursive prefix drilldown over /search/firm to enumerate every
           visible firm CRD. The search endpoint caps at start=9900 hits per
           query, so single-letter prefixes for common letters overflow and
           must be drilled (a -> aa, ab, ..., az, a0, ..., a9, "a "). Results
           land in a temp table _stage_finra_crds_to_fetch.
  phase2 — GET /search/firm/{crd} for each enumerated CRD; parse the
           JSON-encoded _source.content blob; UPSERT one row into
           entities.source_finra_brokercheck_firms.

Audit: ops.finra_brokercheck_ingest_runs.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_finra_brokercheck_firms_ingest.py phase1 [--rate-limit-rps N] [--prefix a,b,c] [--dry-run]
  PYTHONPATH=. doppler run -- python3 scripts/run_finra_brokercheck_firms_ingest.py phase2 [--rate-limit-rps N] [--max-crds N] [--dry-run]
  PYTHONPATH=. doppler run -- python3 scripts/run_finra_brokercheck_firms_ingest.py all [--rate-limit-rps N] [--max-crds N] [--prefix ...] [--dry-run]
  PYTHONPATH=. doppler run -- python3 scripts/run_finra_brokercheck_firms_ingest.py all --recon-only > docs/recon/finra_brokercheck_recon_$(date +%Y%m%d).txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb


API_BASE = "https://api.brokercheck.finra.org"
SEARCH_PATH = "/search/firm"
DETAIL_PATH = "/search/firm/{crd}"

USER_AGENT = "Mozilla/5.0 (Macintosh) data-engine-x/finra-brokercheck-ingest"

DEFAULT_RATE_LIMIT_RPS = 5.0
MAX_DEEP_PAGE_START = 9900
MAX_NROWS = 100
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
DRILLDOWN_DEPTH_CAP = 4

DEFAULT_SEED_PREFIX_ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789")
DRILLDOWN_ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789 ")

DATE_INPUT_FORMATS = ("%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("finra-brokercheck-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


STAGE_TABLE = "ops.finra_brokercheck_stage_crds"


def truncate_stage_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {STAGE_TABLE};")
    conn.commit()


def insert_stage_crds(
    conn: psycopg.Connection, rows: Iterable[tuple[int, str, int | None]]
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {STAGE_TABLE} "
            "(crd_number, discovered_via_prefix, branches_count_from_search) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (crd_number) DO UPDATE SET "
            "  branches_count_from_search = COALESCE("
            f"    EXCLUDED.branches_count_from_search,"
            f"    {STAGE_TABLE}.branches_count_from_search);",
            rows,
        )
    conn.commit()
    return len(rows)


def stage_crd_count(conn: psycopg.Connection, *, status: str | None = None) -> int:
    with conn.cursor() as cur:
        if status:
            cur.execute(
                f"SELECT count(*) FROM {STAGE_TABLE} WHERE stage_status = %s;",
                (status,),
            )
        else:
            cur.execute(f"SELECT count(*) FROM {STAGE_TABLE};")
        return int(cur.fetchone()[0])


def stage_crds_iter(
    conn: psycopg.Connection,
    *,
    max_crds: int | None = None,
    only_pending: bool = True,
) -> Iterable[tuple[int, int | None]]:
    where = "WHERE stage_status = 'pending'" if only_pending else ""
    sql = (f"SELECT crd_number, branches_count_from_search "
           f"FROM {STAGE_TABLE} {where} ORDER BY crd_number")
    if max_crds is not None:
        sql += f" LIMIT {int(max_crds)}"
    with conn.cursor(name=f"stage_crds_cursor_{int(time.monotonic()*1e6)}") as cur:
        cur.itersize = 5000
        cur.execute(sql)
        for row in cur:
            yield (int(row[0]), int(row[1]) if row[1] is not None else None)


def mark_stage_crd(
    conn: psycopg.Connection, crd: int, *, status: str, run_id: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {STAGE_TABLE} "
            "SET stage_status = %s, fetched_at = now(), ingest_run_id = %s "
            "WHERE crd_number = %s;",
            (status, run_id, crd),
        )


# --------------------------------------------------------------------------- #
# Rate-limiter
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Simple sleep-based limiter: never exceeds N requests / second."""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last_call = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


# --------------------------------------------------------------------------- #
# HTTP client + retry
# --------------------------------------------------------------------------- #

@dataclass
class HttpStats:
    total: int = 0
    by_4xx: int = 0
    by_5xx: int = 0


def _fatal_403(url: str, body: str) -> None:
    log.error(
        "ABORTING — FINRA returned 403 for %s. Body: %s. The User-Agent gate "
        "may have tightened. Do not retry — surface this to the directive owner.",
        url, body[:200],
    )
    raise RuntimeError("FINRA API returned 403; aborting per directive policy.")


def _request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    rate: RateLimiter,
    stats: HttpStats,
    params: dict[str, Any] | None = None,
) -> httpx.Response | None:
    """Returns the Response on success (2xx). Returns None on persistent
    non-200 (after retries). Raises RuntimeError on 403 — fatal."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        rate.acquire()
        stats.total += 1
        try:
            r = client.request(method, url, params=params, timeout=30.0)
        except httpx.RequestError as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HTTP %s %s error (%s); retry in %ss",
                        method, url, exc, wait)
            time.sleep(wait)
            continue
        if r.status_code == 403:
            stats.by_4xx += 1
            _fatal_403(url, r.text)  # raises
        if r.status_code in RETRY_STATUSES:
            if 500 <= r.status_code < 600:
                stats.by_5xx += 1
            elif 400 <= r.status_code < 500:
                stats.by_4xx += 1
            wait = min(2 ** attempt, 30)
            log.warning("HTTP %s %s -> %s; retry in %ss",
                        method, url, r.status_code, wait)
            time.sleep(wait)
            continue
        if 200 <= r.status_code < 300:
            return r
        if 400 <= r.status_code < 500:
            stats.by_4xx += 1
        elif 500 <= r.status_code < 600:
            stats.by_5xx += 1
        log.warning("HTTP %s %s -> %s (no retry)", method, url, r.status_code)
        return None
    log.error("HTTP %s %s exhausted retries (%s)", method, url, last_exc)
    return None


# --------------------------------------------------------------------------- #
# Phase 1: recursive prefix enumeration
# --------------------------------------------------------------------------- #

def _search_total(
    client: httpx.Client, query: str, *, rate: RateLimiter, stats: HttpStats
) -> int | None:
    r = _request_with_retries(
        client, "GET", API_BASE + SEARCH_PATH,
        rate=rate, stats=stats,
        params={"query": query, "hl": "false", "nrows": 1, "start": 0,
                "r": 25, "sort": "score+desc"},
    )
    if r is None:
        return None
    try:
        j = r.json()
    except Exception:
        log.warning("search-total %s: JSON parse failed: %s", query, r.text[:200])
        return None
    if j is None:
        return None
    total = (j.get("hits") or {}).get("total")
    return total


def _search_page(
    client: httpx.Client, query: str, *, start: int, nrows: int,
    rate: RateLimiter, stats: HttpStats,
) -> list[dict[str, Any]]:
    r = _request_with_retries(
        client, "GET", API_BASE + SEARCH_PATH,
        rate=rate, stats=stats,
        params={"query": query, "hl": "false", "nrows": nrows, "start": start,
                "r": 25, "sort": "score+desc"},
    )
    if r is None:
        return []
    try:
        j = r.json()
    except Exception:
        log.warning("search-page %s start=%s: JSON parse failed: %s",
                    query, start, r.text[:200])
        return []
    if j is None:
        return []
    return (j.get("hits") or {}).get("hits") or []


def enumerate_crds(
    client: httpx.Client, conn: psycopg.Connection,
    *, seed_prefixes: list[str], rate: RateLimiter, stats: HttpStats,
) -> tuple[int, int]:
    """Recursive drilldown. Returns (unique_crds_discovered, prefixes_drilled).
    Inserts CRDs into the temp _stage_finra_crds_to_fetch as discovered."""
    seen: set[int] = set()
    drilled = 0

    def walk(prefix: str, depth: int) -> None:
        nonlocal drilled
        total = _search_total(client, prefix, rate=rate, stats=stats)
        if total is None:
            log.warning("prefix=%r: total=None (skipping)", prefix)
            return
        if total == 0:
            return
        if total <= MAX_DEEP_PAGE_START and total <= 9900:
            # paginate fully
            log.info("prefix=%r depth=%d total=%d — paginating",
                     prefix, depth, total)
            seen_local = 0
            page_size = MAX_NROWS
            cap = min(total, MAX_DEEP_PAGE_START + 99)  # last full page < cap
            start = 0
            while start < total and start <= MAX_DEEP_PAGE_START:
                this_size = min(page_size, total - start)
                hits = _search_page(
                    client, prefix, start=start, nrows=this_size,
                    rate=rate, stats=stats,
                )
                if not hits:
                    break
                new_rows: list[tuple[int, str, int | None]] = []
                for h in hits:
                    src = h.get("_source") or {}
                    crd_raw = src.get("firm_source_id")
                    if crd_raw is None:
                        continue
                    try:
                        crd = int(crd_raw)
                    except (TypeError, ValueError):
                        continue
                    branches = src.get("firm_branches_count")
                    try:
                        branches = int(branches) if branches is not None else None
                    except (TypeError, ValueError):
                        branches = None
                    if crd in seen:
                        continue
                    seen.add(crd)
                    seen_local += 1
                    new_rows.append((crd, prefix, branches))
                if new_rows:
                    insert_stage_crds(conn, new_rows)
                start += this_size
            if seen_local:
                log.info("prefix=%r yielded %d new CRDs (cumulative=%d)",
                         prefix, seen_local, len(seen))
            return
        # Overflow — drill
        if depth >= DRILLDOWN_DEPTH_CAP:
            log.warning("prefix=%r total=%d at depth %d (drill-cap reached) — "
                        "accepting partial coverage of this branch",
                        prefix, total, depth)
            # Still paginate as much as we can (first 9900)
            page_size = MAX_NROWS
            start = 0
            while start <= MAX_DEEP_PAGE_START:
                hits = _search_page(
                    client, prefix, start=start, nrows=page_size,
                    rate=rate, stats=stats,
                )
                if not hits:
                    break
                new_rows: list[tuple[int, str, int | None]] = []
                for h in hits:
                    src = h.get("_source") or {}
                    crd_raw = src.get("firm_source_id")
                    if crd_raw is None:
                        continue
                    try:
                        crd = int(crd_raw)
                    except (TypeError, ValueError):
                        continue
                    branches = src.get("firm_branches_count")
                    try:
                        branches = int(branches) if branches is not None else None
                    except (TypeError, ValueError):
                        branches = None
                    if crd in seen:
                        continue
                    seen.add(crd)
                    new_rows.append((crd, prefix, branches))
                if new_rows:
                    insert_stage_crds(conn, new_rows)
                start += page_size
            return
        log.info("prefix=%r total=%d > %d — drilling depth=%d",
                 prefix, total, MAX_DEEP_PAGE_START, depth)
        drilled += 1
        for c in DRILLDOWN_ALPHABET:
            walk(prefix + c, depth + 1)

    for seed in seed_prefixes:
        walk(seed, depth=1)

    return len(seen), drilled


# --------------------------------------------------------------------------- #
# Phase 2: per-CRD detail fetch + UPSERT
# --------------------------------------------------------------------------- #

def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for fmt in DATE_INPUT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date() if fmt == "%m/%d/%Y" or fmt == "%Y-%m-%d" else dt.date()
        except ValueError:
            continue
    return None


def _disclosure_count(disclosures: list[dict[str, Any]] | None, kind: str) -> int | None:
    if not disclosures:
        return None
    for d in disclosures:
        if d.get("disclosureType") == kind:
            try:
                return int(d.get("disclosureCount") or 0)
            except (TypeError, ValueError):
                return None
    return 0


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _addr(addr: dict[str, Any] | None) -> tuple[str | None, str | None, str | None,
                                                 str | None, str | None, str | None]:
    if not addr:
        return (None,) * 6
    return (
        addr.get("street1") or None,
        addr.get("street2") or None,
        addr.get("city") or None,
        addr.get("state") or None,
        addr.get("country") or None,
        addr.get("postalCode") or None,
    )


def _extract_columns(content: dict[str, Any]) -> dict[str, Any]:
    bi = content.get("basicInformation") or {}
    addr_details = content.get("firmAddressDetails") or {}
    ia_addr_details = content.get("iaFirmAddressDetails") or {}

    # Prefer firmAddressDetails (the primary BD-side address), fall back to
    # iaFirmAddressDetails for RIA-only firms that have no BD-side filing.
    office = (addr_details.get("officeAddress")
              or ia_addr_details.get("officeAddress"))
    mailing = addr_details.get("mailingAddress")

    o1, o2, oc, os_, ocy, opc = _addr(office)
    m1, m2, mc, ms, mcy, mpc = _addr(mailing)

    other_names = bi.get("otherNames")
    if isinstance(other_names, list):
        other_names = [str(n) for n in other_names if n is not None]
    else:
        other_names = None

    disclosures = content.get("disclosures") or []
    registrations = content.get("registrations") or {}

    crd = bi.get("firmId")
    crd_int = _safe_int(crd)

    return {
        "crd_number": crd_int,
        "firm_name": bi.get("firmName") or bi.get("iaFirmName"),
        "firm_other_names": other_names,
        "bc_scope": bi.get("bcScope"),
        "ia_scope": bi.get("iaScope"),
        "finra_registered": bi.get("finraRegistered"),
        "firm_status": bi.get("firmStatus"),
        "firm_status_date": _parse_date(bi.get("firmStatusDate")),
        "firm_size": bi.get("firmSize"),
        "firm_type": bi.get("firmType"),
        "regulator": bi.get("regulator"),
        "district_name": bi.get("districtName"),
        "finra_last_approval_date": _parse_date(bi.get("finraLastApprovalDate")),
        "formed_date": _parse_date(bi.get("formedDate")),
        "formed_state": bi.get("formedState"),
        "ia_sec_number": bi.get("iaSECNumber"),
        "ia_sec_number_type": bi.get("iaSECNumberType"),
        "bd_sec_number": bi.get("bdSECNumber"),
        "branches_count": _safe_int(bi.get("branchesCount")
                                    or content.get("branchOffices", {}).get("totalBranches")),
        "bd_disclosure_flag": content.get("bdDisclosureFlag"),
        "ia_disclosure_flag": content.get("iaDisclosureFlag"),
        "regulatory_event_count": _disclosure_count(disclosures, "Regulatory Event"),
        "civil_event_count": _disclosure_count(disclosures, "Civil Event"),
        "arbitration_count": _disclosure_count(disclosures, "Arbitration"),
        "approved_finra_registration_count":
            _safe_int(registrations.get("approvedFinraRegistrationCount")),
        "approved_sec_registration_count":
            _safe_int(registrations.get("approvedSECRegistrationCount")),
        "approved_sro_registration_count":
            _safe_int(registrations.get("approvedSRORegistrationCount")),
        "approved_state_registration_count":
            _safe_int(registrations.get("approvedStateRegistrationCount")),
        "office_street1": o1, "office_street2": o2, "office_city": oc,
        "office_state": os_, "office_country": ocy, "office_postal_code": opc,
        "mailing_street1": m1, "mailing_street2": m2, "mailing_city": mc,
        "mailing_state": ms, "mailing_country": mcy, "mailing_postal_code": mpc,
        "business_phone_number": addr_details.get("businessPhoneNumber"),
    }


UPSERT_COLS = [
    "crd_number", "firm_name", "firm_other_names", "bc_scope", "ia_scope",
    "finra_registered", "firm_status", "firm_status_date", "firm_size", "firm_type",
    "regulator", "district_name", "finra_last_approval_date", "formed_date",
    "formed_state", "ia_sec_number", "ia_sec_number_type", "bd_sec_number",
    "branches_count", "bd_disclosure_flag", "ia_disclosure_flag",
    "regulatory_event_count", "civil_event_count", "arbitration_count",
    "approved_finra_registration_count", "approved_sec_registration_count",
    "approved_sro_registration_count", "approved_state_registration_count",
    "office_street1", "office_street2", "office_city", "office_state",
    "office_country", "office_postal_code",
    "mailing_street1", "mailing_street2", "mailing_city", "mailing_state",
    "mailing_country", "mailing_postal_code",
    "business_phone_number", "raw_detail_json", "dataset_fetched_at",
]


def upsert_firm(
    conn: psycopg.Connection, row: dict[str, Any]
) -> str:
    """Returns 'inserted' or 'updated' (or 'unchanged' if no fields differed)."""
    placeholders = ", ".join(["%s"] * len(UPSERT_COLS))
    update_cols = [c for c in UPSERT_COLS if c not in ("crd_number",)]
    update_clause = ",\n  ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    where_clause = " OR ".join(
        f"entities.source_finra_brokercheck_firms.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in update_cols
    )
    sql = f"""
        INSERT INTO entities.source_finra_brokercheck_firms (
            {', '.join(UPSERT_COLS)}
        ) VALUES ({placeholders})
        ON CONFLICT (crd_number) DO UPDATE SET
          {update_clause},
          ingested_at = now()
        WHERE {where_clause}
        RETURNING (xmax = 0) AS inserted;
    """
    values = [row[c] for c in UPSERT_COLS]
    with conn.cursor() as cur:
        cur.execute(sql, values)
        result = cur.fetchone()
    if result is None:
        return "unchanged"
    return "inserted" if result[0] else "updated"


def fetch_detail(
    client: httpx.Client, crd: int,
    *, rate: RateLimiter, stats: HttpStats,
) -> dict[str, Any] | None:
    r = _request_with_retries(
        client, "GET", API_BASE + DETAIL_PATH.format(crd=crd),
        rate=rate, stats=stats,
    )
    if r is None:
        return None
    try:
        j = r.json()
    except Exception:
        log.warning("detail crd=%s: JSON parse failed: %s", crd, r.text[:200])
        return None
    if j is None:
        return None
    hits_obj = j.get("hits") or {}
    if (hits_obj.get("total") or 0) == 0:
        return None
    hits = hits_obj.get("hits") or []
    if not hits:
        return None
    src = hits[0].get("_source") or {}
    content_str = src.get("content")
    if not content_str:
        return None
    try:
        return json.loads(content_str)
    except Exception:
        log.warning("detail crd=%s: content JSON parse failed", crd)
        return None


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #

def insert_run_row(
    conn: psycopg.Connection, *, run_phase: str, rate_limit_rps: float,
    enumeration_strategy: str | None,
) -> str:
    sql = """
        INSERT INTO ops.finra_brokercheck_ingest_runs (
            run_phase, status, source_url_base, enumeration_strategy, rate_limit_rps
        ) VALUES (%s, 'running', %s, %s, %s)
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (run_phase, API_BASE, enumeration_strategy, rate_limit_rps))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *, status: str,
    started_at: float,
    unique_crds_discovered: int | None = None,
    search_calls_total: int | None = None,
    search_calls_4xx: int | None = None,
    search_calls_5xx: int | None = None,
    prefixes_drilled: int | None = None,
    firms_inserted: int | None = None,
    firms_updated: int | None = None,
    firms_unchanged: int | None = None,
    firms_failed: int | None = None,
    detail_calls_total: int | None = None,
    detail_calls_4xx: int | None = None,
    detail_calls_5xx: int | None = None,
    error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    api_total = (search_calls_total or 0) + (detail_calls_total or 0)
    api_4xx = (search_calls_4xx or 0) + (detail_calls_4xx or 0)
    api_5xx = (search_calls_5xx or 0) + (detail_calls_5xx or 0)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.finra_brokercheck_ingest_runs
               SET status = %s,
                   unique_crds_discovered = %s,
                   search_calls_total = %s, search_calls_4xx = %s, search_calls_5xx = %s,
                   prefixes_drilled = %s,
                   firms_inserted = %s, firms_updated = %s,
                   firms_unchanged = %s, firms_failed = %s,
                   detail_calls_total = %s, detail_calls_4xx = %s, detail_calls_5xx = %s,
                   api_calls_total = %s, api_calls_4xx = %s, api_calls_5xx = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, unique_crds_discovered,
            search_calls_total, search_calls_4xx, search_calls_5xx,
            prefixes_drilled,
            firms_inserted, firms_updated, firms_unchanged, firms_failed,
            detail_calls_total, detail_calls_4xx, detail_calls_5xx,
            api_total, api_4xx, api_5xx,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Recon report
# --------------------------------------------------------------------------- #

@dataclass
class ReconStats:
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities.source_finra_brokercheck_firms;")
        s.notes["total_firms"] = int(cur.fetchone()[0])
        if s.notes["total_firms"] == 0:
            return s
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE bc_scope = 'ACTIVE'),
              count(*) FILTER (WHERE bc_scope = 'INACTIVE'),
              count(*) FILTER (WHERE ia_scope = 'ACTIVE'),
              count(*) FILTER (WHERE ia_scope = 'INACTIVE'),
              count(*) FILTER (WHERE bc_scope = 'ACTIVE' AND ia_scope = 'ACTIVE'),
              count(*) FILTER (WHERE bc_scope = 'ACTIVE' AND (ia_scope IS NULL OR ia_scope <> 'ACTIVE')),
              count(*) FILTER (WHERE (bc_scope IS NULL OR bc_scope <> 'ACTIVE') AND ia_scope = 'ACTIVE')
              FROM entities.source_finra_brokercheck_firms;
        """)
        bc_a, bc_i, ia_a, ia_i, dual, bd_only, ria_only = cur.fetchone()
        s.notes["scope_distribution"] = {
            "bc_scope_ACTIVE": int(bc_a),
            "bc_scope_INACTIVE": int(bc_i),
            "ia_scope_ACTIVE": int(ia_a),
            "ia_scope_INACTIVE": int(ia_i),
            "dual_active_bd_and_ria": int(dual),
            "bd_only_active": int(bd_only),
            "ria_only_active": int(ria_only),
        }
        cur.execute("""
            SELECT regulator, count(*) FROM entities.source_finra_brokercheck_firms
            GROUP BY regulator ORDER BY count(*) DESC;
        """)
        s.notes["regulator_distribution"] = [
            {"regulator": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE office_street1 IS NOT NULL AND office_city IS NOT NULL
                               AND office_state IS NOT NULL AND office_postal_code IS NOT NULL
                               AND (office_country IS NULL OR upper(office_country) IN ('UNITED STATES', 'US', 'USA'))),
              count(*) FILTER (WHERE office_street1 IS NOT NULL),
              count(*) FILTER (WHERE mailing_street1 IS NOT NULL),
              count(*) FILTER (WHERE mailing_street1 IS NOT NULL
                               AND (mailing_street1 IS DISTINCT FROM office_street1
                                    OR mailing_city IS DISTINCT FROM office_city
                                    OR mailing_state IS DISTINCT FROM office_state))
              FROM entities.source_finra_brokercheck_firms;
        """)
        full_us, off_any, mail_any, mail_diff = cur.fetchone()
        s.notes["addresses"] = {
            "full_us_office_address": int(full_us),
            "any_office_street": int(off_any),
            "any_mailing_street": int(mail_any),
            "mailing_differs_from_office": int(mail_diff),
        }
        cur.execute("""
            SELECT firm_name, branches_count
              FROM entities.source_finra_brokercheck_firms
             WHERE branches_count IS NOT NULL
             ORDER BY branches_count DESC NULLS LAST
             LIMIT 15;
        """)
        s.notes["top_15_firms_by_branches_count"] = [
            {"firm_name": r[0], "branches": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT office_state, count(*)
              FROM entities.source_finra_brokercheck_firms
             WHERE office_state IS NOT NULL
             GROUP BY office_state ORDER BY count(*) DESC LIMIT 10;
        """)
        s.notes["top_10_office_states"] = [
            {"state": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE formed_date IS NULL),
              count(*) FILTER (WHERE formed_date < DATE '1980-01-01'),
              count(*) FILTER (WHERE formed_date >= DATE '1980-01-01' AND formed_date < DATE '2000-01-01'),
              count(*) FILTER (WHERE formed_date >= DATE '2000-01-01' AND formed_date < DATE '2010-01-01'),
              count(*) FILTER (WHERE formed_date >= DATE '2010-01-01' AND formed_date < DATE '2020-01-01'),
              count(*) FILTER (WHERE formed_date >= DATE '2020-01-01')
              FROM entities.source_finra_brokercheck_firms;
        """)
        unk, pre80, eighty_99, two_thousand_09, twenty_19, twenty_plus = cur.fetchone()
        s.notes["formed_date_buckets"] = {
            "unknown_or_null": int(unk),
            "pre_1980": int(pre80),
            "1980_1999": int(eighty_99),
            "2000_2009": int(two_thousand_09),
            "2010_2019": int(twenty_19),
            "2020_plus": int(twenty_plus),
        }
        cur.execute("""
            SELECT
              run_phase, status,
              api_calls_total, api_calls_4xx, api_calls_5xx,
              duration_seconds, started_at
              FROM ops.finra_brokercheck_ingest_runs
              ORDER BY started_at DESC LIMIT 5;
        """)
        s.notes["recent_runs"] = [
            {
                "phase": r[0], "status": r[1],
                "api_calls_total": int(r[2] or 0),
                "api_calls_4xx": int(r[3] or 0),
                "api_calls_5xx": int(r[4] or 0),
                "duration_seconds": float(r[5]) if r[5] is not None else None,
                "started_at": str(r[6]),
            } for r in cur.fetchall()
        ]
    return s


def print_recon(s: ReconStats) -> None:
    print("=== RECON: entities.source_finra_brokercheck_firms ===")
    for k, v in s.notes.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                if isinstance(vv, int):
                    print(f"      {kk}: {vv:,}")
                else:
                    print(f"      {kk}: {vv}")
        elif isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"      {item}")
        elif isinstance(v, int):
            print(f"  {k}: {v:,}")
        elif isinstance(v, float):
            print(f"  {k}: {v:,.2f}")
        else:
            print(f"  {k}: {v}")
    print("=== END RECON ===\n")


# --------------------------------------------------------------------------- #
# Phase entry-points
# --------------------------------------------------------------------------- #

def run_phase1(
    conn: psycopg.Connection, client: httpx.Client,
    *, rate: RateLimiter, seed_prefixes: list[str], dry_run: bool,
) -> tuple[int, int, HttpStats]:
    stats = HttpStats()
    started_wall = time.monotonic()
    enumeration_strategy = (
        f"recursive-prefix:{''.join(seed_prefixes)};drilldown=[a-z0-9 ];"
        f"depth_cap={DRILLDOWN_DEPTH_CAP}"
    )
    if dry_run:
        log.info("DRY RUN — phase1 will probe prefix totals only, no DB writes")
        for p in seed_prefixes[:5]:
            t = _search_total(client, p, rate=rate, stats=stats)
            log.info("dry-run prefix=%r total=%s", p, t)
        return 0, 0, stats
    run_id = insert_run_row(
        conn, run_phase="phase1", rate_limit_rps=rate.min_interval and 1.0/rate.min_interval or 0,
        enumeration_strategy=enumeration_strategy,
    )
    log.info("phase1 run id=%s strategy=%s seeds=%s",
             run_id, enumeration_strategy, seed_prefixes)
    try:
        log.info("phase1: truncating %s before fresh enumeration", STAGE_TABLE)
        truncate_stage_table(conn)
        unique_crds, drilled = enumerate_crds(
            client, conn, seed_prefixes=seed_prefixes, rate=rate, stats=stats,
        )
        finalize_run_row(
            conn, run_id, status="completed", started_at=started_wall,
            unique_crds_discovered=unique_crds,
            search_calls_total=stats.total,
            search_calls_4xx=stats.by_4xx, search_calls_5xx=stats.by_5xx,
            prefixes_drilled=drilled,
        )
        log.info("phase1 DONE unique_crds=%d search_calls=%d 4xx=%d 5xx=%d "
                 "prefixes_drilled=%d wall=%.1fs",
                 unique_crds, stats.total, stats.by_4xx, stats.by_5xx,
                 drilled, time.monotonic() - started_wall)
        return unique_crds, drilled, stats
    except Exception as exc:
        log.exception("phase1 failed")
        finalize_run_row(
            conn, run_id, status="failed", started_at=started_wall,
            search_calls_total=stats.total,
            search_calls_4xx=stats.by_4xx, search_calls_5xx=stats.by_5xx,
            error_message=str(exc),
        )
        raise


def run_phase2(
    conn: psycopg.Connection, client: httpx.Client,
    *, rate: RateLimiter, max_crds: int | None, dry_run: bool,
) -> tuple[int, int, int, int, HttpStats]:
    stats = HttpStats()
    started_wall = time.monotonic()
    if dry_run:
        log.info("DRY RUN — phase2 will fetch first 3 CRDs and inspect parsed shape only")
        crds_iter = list(stage_crds_iter(conn, max_crds=3))
        for crd, _branches in crds_iter:
            content = fetch_detail(client, crd, rate=rate, stats=stats)
            log.info("dry-run crd=%s content_keys=%s",
                     crd, list((content or {}).keys()))
        return 0, 0, 0, 0, stats

    total_in_stage = stage_crd_count(conn)
    if total_in_stage == 0:
        log.warning("phase2: stage table is empty — run phase1 first")
        return 0, 0, 0, 0, stats
    run_id = insert_run_row(
        conn, run_phase="phase2",
        rate_limit_rps=(1.0 / rate.min_interval) if rate.min_interval else 0,
        enumeration_strategy=None,
    )
    log.info("phase2 run id=%s total_in_stage=%d max_crds=%s",
             run_id, total_in_stage, max_crds)

    inserted = updated = unchanged = failed = 0
    try:
        progress_interval = 500
        last_log = time.monotonic()
        last_processed = 0
        # The stage_crds_iter uses a server-side cursor on the same conn
        # we're committing on. Materialize CRDs up front so commits don't
        # invalidate the cursor.
        crds_to_process = list(
            stage_crds_iter(conn, max_crds=max_crds, only_pending=True)
        )
        log.info("phase2: %d CRDs pending in stage table", len(crds_to_process))
        for processed, (crd, branches_from_search) in enumerate(
            crds_to_process, start=1
        ):
            content = fetch_detail(client, crd, rate=rate, stats=stats)
            if content is None:
                failed += 1
                mark_stage_crd(conn, crd, status="failed", run_id=run_id)
                conn.commit()
                continue
            row = _extract_columns(content)
            row["raw_detail_json"] = Jsonb(content)
            row["dataset_fetched_at"] = datetime.now(timezone.utc)
            if row["crd_number"] is None:
                row["crd_number"] = crd
            # Detail endpoint never returns branchesCount; use the value
            # captured from the phase-1 search-result light document.
            if row.get("branches_count") is None and branches_from_search is not None:
                row["branches_count"] = branches_from_search
            try:
                outcome = upsert_firm(conn, row)
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1
                mark_stage_crd(conn, crd, status="fetched", run_id=run_id)
            except Exception as upsert_exc:
                conn.rollback()
                failed += 1
                mark_stage_crd(conn, crd, status="failed", run_id=run_id)
                conn.commit()
                log.warning("crd=%s UPSERT failed: %s", crd, upsert_exc)
                continue
            else:
                conn.commit()
            if processed % progress_interval == 0:
                now = time.monotonic()
                rate_observed = (processed - last_processed) / max(now - last_log, 1e-3)
                log.info("phase2 progress: processed=%d/%s inserted=%d updated=%d "
                         "unchanged=%d failed=%d rate_obs=%.1f req/s 4xx=%d 5xx=%d",
                         processed, total_in_stage, inserted, updated, unchanged, failed,
                         rate_observed, stats.by_4xx, stats.by_5xx)
                last_log = now
                last_processed = processed

        finalize_run_row(
            conn, run_id, status="completed", started_at=started_wall,
            firms_inserted=inserted, firms_updated=updated,
            firms_unchanged=unchanged, firms_failed=failed,
            detail_calls_total=stats.total,
            detail_calls_4xx=stats.by_4xx, detail_calls_5xx=stats.by_5xx,
        )
        log.info("phase2 DONE ins=%d upd=%d unch=%d failed=%d detail_calls=%d "
                 "4xx=%d 5xx=%d wall=%.1fs",
                 inserted, updated, unchanged, failed, stats.total,
                 stats.by_4xx, stats.by_5xx,
                 time.monotonic() - started_wall)
        return inserted, updated, unchanged, failed, stats
    except Exception as exc:
        log.exception("phase2 failed")
        finalize_run_row(
            conn, run_id, status="failed", started_at=started_wall,
            firms_inserted=inserted, firms_updated=updated,
            firms_unchanged=unchanged, firms_failed=failed,
            detail_calls_total=stats.total,
            detail_calls_4xx=stats.by_4xx, detail_calls_5xx=stats.by_5xx,
            error_message=str(exc),
        )
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["phase1", "phase2", "all"],
                   help="Which phase to run.")
    p.add_argument("--rate-limit-rps", type=float, default=DEFAULT_RATE_LIMIT_RPS,
                   help=f"Max requests per second. Default {DEFAULT_RATE_LIMIT_RPS}.")
    p.add_argument("--prefix",
                   help="Comma-separated seed prefixes for phase1 "
                        "(default: a-z + 0-9).")
    p.add_argument("--max-crds", type=int, default=None,
                   help="Phase 2 only: stop after fetching N firms (smoke testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Phase 1: probe a few prefixes only. Phase 2: fetch first "
                        "3 CRDs and inspect, no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing data and exit. "
                        "Skips phase1/phase2.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        with psycopg.connect(_database_url()) as conn:
            try:
                s = gather_recon(conn)
                print_recon(s)
            except psycopg.errors.UndefinedTable:
                log.error("Tables missing — apply the migration first.")
                return 2
        return 0

    seed_prefixes = (
        [p.strip() for p in args.prefix.split(",") if p.strip()]
        if args.prefix else list(DEFAULT_SEED_PREFIX_ALPHABET)
    )
    rate = RateLimiter(args.rate_limit_rps)

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    rc = 0
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        # Phase1 + phase2 share a single connection so the temp stage table
        # persists between them when running 'all'.
        with psycopg.connect(_database_url()) as conn:
            if args.phase in ("phase1", "all"):
                try:
                    run_phase1(
                        conn, client, rate=rate, seed_prefixes=seed_prefixes,
                        dry_run=args.dry_run,
                    )
                except Exception:
                    rc = 1
                    if args.phase == "phase1":
                        return rc
            if args.phase in ("phase2", "all"):
                try:
                    run_phase2(
                        conn, client, rate=rate,
                        max_crds=args.max_crds, dry_run=args.dry_run,
                    )
                except Exception:
                    rc = 1

        # End-of-run recon (skip in dry-run since DB has no new rows).
        if not args.dry_run:
            with psycopg.connect(_database_url()) as conn2:
                try:
                    s = gather_recon(conn2)
                    print_recon(s)
                except psycopg.errors.UndefinedTable:
                    pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
