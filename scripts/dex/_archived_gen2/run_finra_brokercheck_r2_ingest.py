#!/usr/bin/env python3
"""FINRA BrokerCheck → R2 Fuel Tank ingest (firms + individuals).

Mirrors the FINRA BrokerCheck registry — broker-dealer firms (~85K) and
individual registered representatives (~600K) — into Cloudflare R2 as
ZSTD-compressed Parquet, snapshot-partitioned by ingest date.

Source: public JSON API at https://api.brokercheck.finra.org

  /search/firm                — firm enumeration (prefix drilldown)
  /search/firm/{crd}          — firm detail
  /search/individual          — individual enumeration (prefix drilldown)
  /search/individual/{crd}    — individual detail

Two phases per kind (firm | individual):

  Phase 1: recursive prefix drilldown over /search/{kind}. The search
           endpoint caps at start=9900 hits, so common prefixes overflow
           and are drilled to two-letter, three-letter, etc. (depth cap 4).
           Lifted from `run_finra_brokercheck_firms_ingest.py`.

  Phase 2: GET /search/{kind}/{crd} per CRD; parse _source.content
           (JSON-encoded string); project to typed columns + raw_payload;
           buffer batches; flush as part-NNNNN.parquet to a staging dir;
           after all CRDs done, concat all parts via DuckDB into a single
           ZSTD Parquet and upload to R2.

R2 layout:
  finra-brokercheck/
    snapshot=YYYY-MM-DD/
      firms.parquet         (~85K rows)
      individuals.parquet   (~600K rows)

Audit ledger: ops.finra_brokercheck_r2_ingest_runs.

Resumability: enumeration output stashed to {workdir}/{kind}_crds.json;
detail-fetch output flushed incrementally to
{workdir}/{kind}_staging/part-NNNNN.parquet. Re-runs skip enumeration
when the CRD JSON exists and skip detail-fetch for CRDs already present
in staging Parquet parts.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_finra_brokercheck_r2_ingest.py \\
      --kind firms --max-crds 1000 \\
      --r2-prefix-override 'finra-brokercheck/_smoke/firms_1000'

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_finra_brokercheck_r2_ingest.py \\
      --kind both

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_finra_brokercheck_r2_ingest.py \\
      --kind individuals --skip-enumerate  # resume after a crash

See directive
~/Desktop/hq/directives/2026-05-08-finra-brokercheck-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
import duckdb
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.finra_normalize import (  # type: ignore[import-not-found]  # noqa: E402
    extract_branch_zip_state,
    extract_current_employer_crd,
    normalize_firm_name,
    normalize_person_name_part,
    normalize_state,
    zip5,
)


API_BASE = "https://api.brokercheck.finra.org"
USER_AGENT = "data-engine-x/finra-brokercheck-r2-ingest"
R2_BUCKET = "dex-raw-landing-zone"

DEFAULT_RATE_LIMIT_RPS = 5.0
MAX_DEEP_PAGE_START = 9900
MAX_NROWS = 100
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
DRILLDOWN_DEPTH_CAP = 4

DEFAULT_SEED_PREFIX_ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789")
DRILLDOWN_ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789 ")

DEFAULT_BATCH_ROWS = 10_000

# Detail-fetch concurrency multiplies the polite-rate cap; with a single
# httpx.Client the rate limiter still serializes total RPS.

DATE_INPUT_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S")


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("finra-brokercheck-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env / clients
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# Rate-limiter + HTTP retry (lifted verbatim from
# scripts/run_finra_brokercheck_firms_ingest.py — keep behavior identical)
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Sleep-based limiter: never exceeds N requests / second."""

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


@dataclass
class HttpStats:
    total: int = 0
    by_4xx: int = 0
    by_5xx: int = 0


def _fatal_403(url: str, body: str) -> None:
    log.error(
        "ABORTING — FINRA returned 403 for %s. Body: %s. The User-Agent gate "
        "may have tightened. Do not retry — surface to the directive owner.",
        url, body[:200],
    )
    raise RuntimeError("FINRA API returned 403; aborting per directive policy.")


def _request_with_retries(
    client: httpx.Client, method: str, url: str,
    *, rate: RateLimiter, stats: HttpStats,
    params: dict[str, Any] | None = None,
) -> httpx.Response | None:
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
            _fatal_403(url, r.text)
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
# Phase 1: prefix-drilldown enumeration (parameterized by kind)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KindSpec:
    """Parameters that change between firm vs individual."""
    name: str  # 'firms' | 'individuals'
    search_path: str  # '/search/firm' | '/search/individual'
    detail_path: str  # '/search/firm/{crd}' | '/search/individual/{crd}'
    crd_field_in_search: str  # 'firm_source_id' | 'ind_source_id'

    @property
    def search_url(self) -> str:
        return API_BASE + self.search_path

    def detail_url(self, crd: int) -> str:
        return API_BASE + self.detail_path.format(crd=crd)


FIRM_SPEC = KindSpec(
    name="firms",
    search_path="/search/firm",
    detail_path="/search/firm/{crd}",
    crd_field_in_search="firm_source_id",
)

INDIVIDUAL_SPEC = KindSpec(
    name="individuals",
    search_path="/search/individual",
    detail_path="/search/individual/{crd}",
    crd_field_in_search="ind_source_id",
)


def _search_total(
    client: httpx.Client, spec: KindSpec, query: str,
    *, rate: RateLimiter, stats: HttpStats,
) -> int | None:
    r = _request_with_retries(
        client, "GET", spec.search_url,
        rate=rate, stats=stats,
        params={"query": query, "hl": "false", "nrows": 1, "start": 0,
                "r": 25, "sort": "score+desc"},
    )
    if r is None:
        return None
    try:
        j = r.json()
    except Exception:
        log.warning("search-total %s %s: JSON parse failed: %s",
                    spec.name, query, r.text[:200])
        return None
    if j is None:
        return None
    return (j.get("hits") or {}).get("total")


def _search_page(
    client: httpx.Client, spec: KindSpec, query: str,
    *, start: int, nrows: int,
    rate: RateLimiter, stats: HttpStats,
) -> list[dict[str, Any]]:
    r = _request_with_retries(
        client, "GET", spec.search_url,
        rate=rate, stats=stats,
        params={"query": query, "hl": "false", "nrows": nrows, "start": start,
                "r": 25, "sort": "score+desc"},
    )
    if r is None:
        return []
    try:
        j = r.json()
    except Exception:
        log.warning("search-page %s %s start=%s: JSON parse failed: %s",
                    spec.name, query, start, r.text[:200])
        return []
    if j is None:
        return []
    return (j.get("hits") or {}).get("hits") or []


def enumerate_crds(
    client: httpx.Client, spec: KindSpec,
    *, seed_prefixes: list[str], rate: RateLimiter, stats: HttpStats,
) -> tuple[list[int], int]:
    """Recursive drilldown. Returns (sorted_unique_crds, prefixes_drilled).

    Identical control flow to the firms scraper's `enumerate_crds` —
    parameterized over `spec` so individual enumeration uses the same
    prefix-drilldown depth cap and pagination loop.
    """
    seen: set[int] = set()
    drilled = 0

    def collect_hits(hits: list[dict[str, Any]]) -> None:
        for h in hits:
            src = h.get("_source") or {}
            crd_raw = src.get(spec.crd_field_in_search)
            if crd_raw is None or crd_raw == "":
                continue
            try:
                crd = int(crd_raw)
            except (TypeError, ValueError):
                continue
            seen.add(crd)

    def walk(prefix: str, depth: int) -> None:
        nonlocal drilled
        total = _search_total(client, spec, prefix, rate=rate, stats=stats)
        if total is None:
            log.warning("[%s] prefix=%r: total=None (skipping)", spec.name, prefix)
            return
        if total == 0:
            return
        if total <= MAX_DEEP_PAGE_START:
            log.info("[%s] prefix=%r depth=%d total=%d — paginating",
                     spec.name, prefix, depth, total)
            seen_before = len(seen)
            page_size = MAX_NROWS
            start = 0
            while start < total and start <= MAX_DEEP_PAGE_START:
                this_size = min(page_size, total - start)
                hits = _search_page(
                    client, spec, prefix, start=start, nrows=this_size,
                    rate=rate, stats=stats,
                )
                if not hits:
                    break
                collect_hits(hits)
                start += this_size
            log.info("[%s] prefix=%r yielded %d new (cumulative=%d)",
                     spec.name, prefix, len(seen) - seen_before, len(seen))
            return
        if depth >= DRILLDOWN_DEPTH_CAP:
            log.warning("[%s] prefix=%r total=%d at depth %d (drill-cap reached) — "
                        "accepting partial coverage of this branch",
                        spec.name, prefix, total, depth)
            page_size = MAX_NROWS
            start = 0
            while start <= MAX_DEEP_PAGE_START:
                hits = _search_page(
                    client, spec, prefix, start=start, nrows=page_size,
                    rate=rate, stats=stats,
                )
                if not hits:
                    break
                collect_hits(hits)
                start += page_size
            return
        log.info("[%s] prefix=%r total=%d > %d — drilling depth=%d",
                 spec.name, prefix, total, MAX_DEEP_PAGE_START, depth)
        drilled += 1
        for c in DRILLDOWN_ALPHABET:
            walk(prefix + c, depth + 1)

    for seed in seed_prefixes:
        walk(seed, depth=1)

    return sorted(seen), drilled


# --------------------------------------------------------------------------- #
# Phase 2: detail-fetch + record projection
# --------------------------------------------------------------------------- #


def fetch_detail(
    client: httpx.Client, spec: KindSpec, crd: int,
    *, rate: RateLimiter, stats: HttpStats,
) -> dict[str, Any] | None:
    """Returns the inner content dict (the second-level JSON parse).

    Returns None on persistent fetch failure or empty response.
    """
    r = _request_with_retries(
        client, "GET", spec.detail_url(crd), rate=rate, stats=stats,
    )
    if r is None:
        return None
    try:
        j = r.json()
    except Exception:
        log.warning("[%s] detail crd=%s: outer JSON parse failed: %s",
                    spec.name, crd, r.text[:200])
        return None
    hits_obj = (j or {}).get("hits") or {}
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
        log.warning("[%s] detail crd=%s: inner content JSON parse failed",
                    spec.name, crd)
        return None


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _disclosure_count(disclosures: Any, kind: str) -> int | None:
    if not isinstance(disclosures, list):
        return None
    for d in disclosures:
        if isinstance(d, dict) and d.get("disclosureType") == kind:
            try:
                return int(d.get("disclosureCount") or 0)
            except (TypeError, ValueError):
                return None
    return 0


def _addr(addr: Any) -> tuple[
    str | None, str | None, str | None, str | None, str | None, str | None
]:
    if not isinstance(addr, dict):
        return (None,) * 6
    return (
        addr.get("street1") or None,
        addr.get("street2") or None,
        addr.get("city") or None,
        addr.get("state") or None,
        addr.get("country") or None,
        addr.get("postalCode") or None,
    )


def _to_str_list(v: Any) -> list[str] | None:
    if not isinstance(v, list):
        return None
    out = [str(x) for x in v if x is not None]
    return out or None


def _json_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, dict)) and not v:
        return None
    try:
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def project_firm_record(
    crd: int, content: dict[str, Any], snapshot_date: date,
) -> dict[str, Any]:
    """Build the typed projection of a firm detail blob, identical in shape
    to the typed columns on `entities.source_finra_brokercheck_firms`."""
    bi = content.get("basicInformation") or {}
    addr_details = content.get("firmAddressDetails") or {}
    ia_addr_details = content.get("iaFirmAddressDetails") or {}
    office = (addr_details.get("officeAddress")
              or ia_addr_details.get("officeAddress"))
    mailing = addr_details.get("mailingAddress")
    o1, o2, oc, os_, ocy, opc = _addr(office)
    m1, m2, mc, ms, mcy, mpc = _addr(mailing)

    disclosures = content.get("disclosures") or []
    registrations = content.get("registrations") or {}

    firm_name = bi.get("firmName") or bi.get("iaFirmName")
    other_names = _to_str_list(bi.get("otherNames"))

    return {
        "firm_crd": crd,
        "firm_name": firm_name,
        "firm_other_names": _json_or_none(other_names),
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
        "branches_count": _safe_int(
            bi.get("branchesCount")
            or (content.get("branchOffices") or {}).get("totalBranches")
        ),
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
        "raw_payload": _json_or_none(content),
        "finra_snapshot_date": snapshot_date,
        "firm_name_normalized": normalize_firm_name(firm_name),
        "firm_main_office_zip5": zip5(opc),
        "firm_main_office_state_normalized": normalize_state(os_),
    }


def project_individual_record(
    crd: int, content: dict[str, Any], snapshot_date: date,
) -> dict[str, Any]:
    """Build the typed projection of an individual detail blob."""
    bi = content.get("basicInformation") or {}
    current_employments = content.get("currentEmployments") or []
    current_ia_employments = content.get("currentIAEmployments") or []
    previous_employments = content.get("previousEmployments") or []
    previous_ia_employments = content.get("previousIAEmployments") or []
    disclosures = content.get("disclosures") or []
    exams_count = content.get("examsCount") or {}
    registration_count = content.get("registrationCount") or {}
    registered_states = content.get("registeredStates") or []
    registered_sros = content.get("registeredSROs") or []

    first_name = bi.get("firstName")
    middle_name = bi.get("middleName")
    last_name = bi.get("lastName")
    other_names = _to_str_list(bi.get("otherNames"))

    employer_crd = extract_current_employer_crd(current_employments)
    branch_zip_raw, branch_state_raw = extract_branch_zip_state(current_employments)

    return {
        "individual_crd": crd,
        "individual_first_name": first_name,
        "individual_middle_name": middle_name,
        "individual_last_name": last_name,
        "individual_other_names": _json_or_none(other_names),
        "bc_scope": bi.get("bcScope"),
        "ia_scope": bi.get("iaScope"),
        "days_in_industry_calculated_date":
            _parse_date(bi.get("daysInIndustryCalculatedDate")),
        "bc_disclosure_flag": content.get("disclosureFlag"),
        "ia_disclosure_flag": content.get("iaDisclosureFlag"),
        "approved_finra_registration_count":
            _safe_int(registration_count.get("approvedFINRARegistrationCount")
                      or registration_count.get("approvedFinraRegistrationCount")),
        "approved_sec_registration_count":
            _safe_int(registration_count.get("approvedSECRegistrationCount")),
        "approved_sro_registration_count":
            _safe_int(registration_count.get("approvedSRORegistrationCount")),
        "approved_state_registration_count":
            _safe_int(registration_count.get("approvedStateRegistrationCount")),
        "registered_states": _json_or_none(registered_states),
        "registered_sros": _json_or_none(registered_sros),
        "state_exam_count": _safe_int(exams_count.get("stateExamCount")),
        "principal_exam_count": _safe_int(exams_count.get("principalExamCount")),
        "product_exam_count": _safe_int(exams_count.get("productExamCount")),
        "current_employments_count": len(current_employments),
        "previous_employments_count": len(previous_employments),
        "current_employments": _json_or_none(current_employments),
        "current_ia_employments": _json_or_none(current_ia_employments),
        "previous_employments": _json_or_none(previous_employments),
        "previous_ia_employments": _json_or_none(previous_ia_employments),
        "disclosures": _json_or_none(disclosures),
        "raw_payload": _json_or_none(content),
        "finra_snapshot_date": snapshot_date,
        "individual_first_normalized": normalize_person_name_part(first_name),
        "individual_last_normalized": normalize_person_name_part(last_name),
        "current_employer_crd": employer_crd,
        "branch_zip5": zip5(branch_zip_raw),
        "branch_state_normalized": normalize_state(branch_state_raw),
    }


# --------------------------------------------------------------------------- #
# pyarrow schemas
# --------------------------------------------------------------------------- #


_FIRM_SCHEMA = pa.schema([
    ("firm_crd", pa.int64()),
    ("firm_name", pa.string()),
    ("firm_other_names", pa.string()),
    ("bc_scope", pa.string()),
    ("ia_scope", pa.string()),
    ("finra_registered", pa.string()),
    ("firm_status", pa.string()),
    ("firm_status_date", pa.date32()),
    ("firm_size", pa.string()),
    ("firm_type", pa.string()),
    ("regulator", pa.string()),
    ("district_name", pa.string()),
    ("finra_last_approval_date", pa.date32()),
    ("formed_date", pa.date32()),
    ("formed_state", pa.string()),
    ("ia_sec_number", pa.string()),
    ("ia_sec_number_type", pa.string()),
    ("bd_sec_number", pa.string()),
    ("branches_count", pa.int32()),
    ("bd_disclosure_flag", pa.string()),
    ("ia_disclosure_flag", pa.string()),
    ("regulatory_event_count", pa.int32()),
    ("civil_event_count", pa.int32()),
    ("arbitration_count", pa.int32()),
    ("approved_finra_registration_count", pa.int32()),
    ("approved_sec_registration_count", pa.int32()),
    ("approved_sro_registration_count", pa.int32()),
    ("approved_state_registration_count", pa.int32()),
    ("office_street1", pa.string()),
    ("office_street2", pa.string()),
    ("office_city", pa.string()),
    ("office_state", pa.string()),
    ("office_country", pa.string()),
    ("office_postal_code", pa.string()),
    ("mailing_street1", pa.string()),
    ("mailing_street2", pa.string()),
    ("mailing_city", pa.string()),
    ("mailing_state", pa.string()),
    ("mailing_country", pa.string()),
    ("mailing_postal_code", pa.string()),
    ("business_phone_number", pa.string()),
    ("raw_payload", pa.string()),
    ("finra_snapshot_date", pa.date32()),
    ("firm_name_normalized", pa.string()),
    ("firm_main_office_zip5", pa.string()),
    ("firm_main_office_state_normalized", pa.string()),
])


_INDIVIDUAL_SCHEMA = pa.schema([
    ("individual_crd", pa.int64()),
    ("individual_first_name", pa.string()),
    ("individual_middle_name", pa.string()),
    ("individual_last_name", pa.string()),
    ("individual_other_names", pa.string()),
    ("bc_scope", pa.string()),
    ("ia_scope", pa.string()),
    ("days_in_industry_calculated_date", pa.date32()),
    ("bc_disclosure_flag", pa.string()),
    ("ia_disclosure_flag", pa.string()),
    ("approved_finra_registration_count", pa.int32()),
    ("approved_sec_registration_count", pa.int32()),
    ("approved_sro_registration_count", pa.int32()),
    ("approved_state_registration_count", pa.int32()),
    ("registered_states", pa.string()),
    ("registered_sros", pa.string()),
    ("state_exam_count", pa.int32()),
    ("principal_exam_count", pa.int32()),
    ("product_exam_count", pa.int32()),
    ("current_employments_count", pa.int32()),
    ("previous_employments_count", pa.int32()),
    ("current_employments", pa.string()),
    ("current_ia_employments", pa.string()),
    ("previous_employments", pa.string()),
    ("previous_ia_employments", pa.string()),
    ("disclosures", pa.string()),
    ("raw_payload", pa.string()),
    ("finra_snapshot_date", pa.date32()),
    ("individual_first_normalized", pa.string()),
    ("individual_last_normalized", pa.string()),
    ("current_employer_crd", pa.int64()),
    ("branch_zip5", pa.string()),
    ("branch_state_normalized", pa.string()),
])


def _records_to_table(
    records: list[dict[str, Any]], schema: pa.Schema,
) -> pa.Table:
    cols: dict[str, list[Any]] = {f.name: [] for f in schema}
    for r in records:
        for f in schema:
            cols[f.name].append(r.get(f.name))
    return pa.table(cols, schema=schema)


# --------------------------------------------------------------------------- #
# Staging layout helpers
# --------------------------------------------------------------------------- #


def _kind_workdir(workdir: Path, kind: str) -> Path:
    p = workdir / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stage_dir(workdir: Path, kind: str) -> Path:
    p = _kind_workdir(workdir, kind) / "detail_staging"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _crds_path(workdir: Path, kind: str) -> Path:
    return _kind_workdir(workdir, kind) / "crds.json"


def _final_parquet_path(workdir: Path, kind: str) -> Path:
    return _kind_workdir(workdir, kind) / f"{kind}.parquet"


def save_crds(workdir: Path, kind: str, crds: list[int]) -> None:
    p = _crds_path(workdir, kind)
    payload = {
        "kind": kind,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(crds),
        "crds": crds,
    }
    p.write_text(json.dumps(payload, separators=(",", ":")))
    log.info("[%s] saved %d enumerated CRDs → %s", kind, len(crds), p)


def load_crds(workdir: Path, kind: str) -> list[int]:
    p = _crds_path(workdir, kind)
    if not p.exists():
        return []
    payload = json.loads(p.read_text())
    crds = [int(c) for c in payload.get("crds") or []]
    log.info("[%s] loaded %d enumerated CRDs from %s", kind, len(crds), p)
    return crds


def fetched_crds_in_staging(stage: Path, schema: pa.Schema) -> set[int]:
    """Read every staged part-NNNNN.parquet and collect the set of CRDs
    already fetched. Used to skip already-fetched CRDs on resume."""
    parts = sorted(stage.glob("part-*.parquet"))
    if not parts:
        return set()
    pk = "firm_crd" if schema.names[0] == "firm_crd" else "individual_crd"
    seen: set[int] = set()
    for p in parts:
        try:
            tbl = pq.read_table(p, columns=[pk])
            seen.update(int(v) for v in tbl.column(pk).to_pylist() if v is not None)
        except Exception as exc:
            log.warning("staging part %s unreadable (%s); ignoring on resume", p, exc)
    log.info("staging dir %s: %d already-fetched CRDs across %d parts",
             stage, len(seen), len(parts))
    return seen


def next_part_index(stage: Path) -> int:
    parts = sorted(stage.glob("part-*.parquet"))
    if not parts:
        return 0
    last = parts[-1].stem  # 'part-00042'
    try:
        return int(last.split("-", 1)[1]) + 1
    except (IndexError, ValueError):
        return len(parts)


# --------------------------------------------------------------------------- #
# Detail-fetch loop
# --------------------------------------------------------------------------- #


@dataclass
class DetailFetchStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    http_4xx: int = 0
    http_5xx: int = 0
    rows_buffered: int = 0
    parts_written: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def detail_fetch_loop(
    client: httpx.Client, spec: KindSpec, crds: list[int],
    *, rate: RateLimiter, http_stats: HttpStats,
    schema: pa.Schema,
    project_fn,
    snapshot_date: date,
    stage_dir: Path,
    batch_rows: int,
    skip_already_fetched: bool,
) -> DetailFetchStats:
    if skip_already_fetched:
        already = fetched_crds_in_staging(stage_dir, schema)
        if already:
            crds = [c for c in crds if c not in already]
            log.info("[%s] resume: %d CRDs remaining after skipping already-fetched",
                     spec.name, len(crds))

    stats = DetailFetchStats()
    buffer: list[dict[str, Any]] = []
    part_idx = next_part_index(stage_dir)

    def flush() -> None:
        nonlocal part_idx
        if not buffer:
            return
        tbl = _records_to_table(buffer, schema)
        out_path = stage_dir / f"part-{part_idx:05d}.parquet"
        pq.write_table(
            tbl, out_path,
            compression="zstd", compression_level=9,
            row_group_size=10_000,
        )
        log.info("[%s] flushed part-%05d (%d rows, %.1f MB)",
                 spec.name, part_idx, len(buffer),
                 out_path.stat().st_size / (1 << 20))
        part_idx += 1
        stats.parts_written += 1
        buffer.clear()

    last_log = time.monotonic()
    last_processed = 0
    progress_interval = 1000

    for i, crd in enumerate(crds, start=1):
        stats.total += 1
        content = fetch_detail(client, spec, crd, rate=rate, stats=http_stats)
        if content is None:
            stats.failed += 1
            continue
        try:
            row = project_fn(crd, content, snapshot_date)
        except Exception as exc:
            stats.failed += 1
            log.warning("[%s] project crd=%s failed: %s", spec.name, crd, exc)
            continue
        buffer.append(row)
        stats.success += 1
        stats.rows_buffered += 1

        if len(buffer) >= batch_rows:
            flush()

        if i % progress_interval == 0:
            now = time.monotonic()
            elapsed = max(now - last_log, 1e-3)
            rate_obs = (i - last_processed) / elapsed
            log.info("[%s] phase2 progress: processed=%d/%d success=%d failed=%d "
                     "rate_obs=%.1f req/s 4xx=%d 5xx=%d",
                     spec.name, i, len(crds), stats.success, stats.failed,
                     rate_obs, http_stats.by_4xx, http_stats.by_5xx)
            last_log = now
            last_processed = i

    flush()
    stats.http_4xx = http_stats.by_4xx
    stats.http_5xx = http_stats.by_5xx
    return stats


# --------------------------------------------------------------------------- #
# Concat staging parts → single Parquet → R2
# --------------------------------------------------------------------------- #


def concat_staging_to_final(
    stage_dir: Path, final_path: Path, *, schema: pa.Schema, kind: str,
) -> tuple[int, int]:
    """Read all staged part-*.parquet and write a single ZSTD Parquet at
    final_path. Returns (row_count, file_bytes)."""
    parts = sorted(stage_dir.glob("part-*.parquet"))
    if not parts:
        raise RuntimeError(f"no staging parts found at {stage_dir}")
    log.info("[%s] concat %d staging parts → %s", kind, len(parts), final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    glob = str(stage_dir / "part-*.parquet")
    pk = schema.names[0]
    con.execute(f"""
        COPY (
          SELECT * FROM read_parquet('{glob}')
          ORDER BY {pk}
        ) TO '{final_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    con.close()
    rows_row = pq.read_metadata(final_path)
    return rows_row.num_rows, final_path.stat().st_size


def upload_to_r2(local: Path, *, key: str) -> int:
    s3 = _r2_client()
    s3.upload_file(
        str(local), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return local.stat().st_size


def list_existing_r2(prefix: str) -> list[str]:
    s3 = _r2_client()
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents") or []:
            out.append(obj["Key"])
    return out


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection, *,
    snapshot_date: date, table_name: str,
    enumeration_strategy: str, rate_limit_rps: float,
) -> str:
    sql = """
        INSERT INTO ops.finra_brokercheck_r2_ingest_runs (
            snapshot_date, table_name, status,
            source_url_base, enumeration_strategy, rate_limit_rps
        ) VALUES (%s, %s, 'running', %s, %s, %s)
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot_date, table_name, API_BASE,
            enumeration_strategy, rate_limit_rps,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *,
    status: str,
    started_wall: float,
    unique_crds: int | None,
    search_calls_total: int | None,
    search_calls_4xx: int | None,
    search_calls_5xx: int | None,
    prefixes_drilled: int | None,
    detail_calls_total: int | None,
    detail_calls_success: int | None,
    detail_calls_failed: int | None,
    detail_calls_4xx: int | None,
    detail_calls_5xx: int | None,
    parquet_row_count: int | None,
    parquet_bytes_written: int | None,
    parquet_column_count: int | None,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_key: str | None,
    r2_total_bytes: int | None,
    primary_id_null_pct: float | None,
    primary_name_null_pct: float | None,
    current_employer_crd_null_pct: float | None,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.finra_brokercheck_r2_ingest_runs
               SET status = %s,
                   unique_crds_enumerated = %s,
                   search_calls_total = %s,
                   search_calls_4xx = %s,
                   search_calls_5xx = %s,
                   prefixes_drilled = %s,
                   detail_calls_total = %s,
                   detail_calls_success = %s,
                   detail_calls_failed = %s,
                   detail_calls_4xx = %s,
                   detail_calls_5xx = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s,
                   r2_prefix = %s,
                   r2_object_key = %s,
                   r2_total_bytes = %s,
                   primary_id_null_pct = %s,
                   primary_name_null_pct = %s,
                   current_employer_crd_null_pct = %s,
                   finished_at = now(),
                   duration_seconds = %s,
                   error_message = %s,
                   notes = %s
             WHERE id = %s;
        """, (
            status,
            unique_crds,
            search_calls_total, search_calls_4xx, search_calls_5xx,
            prefixes_drilled,
            detail_calls_total, detail_calls_success, detail_calls_failed,
            detail_calls_4xx, detail_calls_5xx,
            parquet_row_count, parquet_bytes_written, parquet_column_count,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            primary_id_null_pct, primary_name_null_pct,
            current_employer_crd_null_pct,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-kind orchestration
# --------------------------------------------------------------------------- #


def compute_null_rates(
    final_path: Path, kind: str,
) -> tuple[float, float, float | None]:
    """Read the final Parquet and compute the directive's per-kind null
    rates: (primary_id_null_pct, primary_name_null_pct,
    current_employer_crd_null_pct).

    For firms: name = firm_name_normalized; employer_crd is None.
    For individuals: name = both first + last NULL; employer_crd =
                     current_employer_crd NULL rate.
    """
    con = duckdb.connect(":memory:")
    try:
        if kind == "firms":
            row = con.execute(f"""
                SELECT
                  count(*) AS total,
                  count(*) FILTER (WHERE firm_crd IS NULL) AS id_null,
                  count(*) FILTER (WHERE firm_name_normalized IS NULL) AS name_null
                FROM read_parquet('{final_path}');
            """).fetchone()
            total = int(row[0]) if row else 0
            if total == 0:
                return 0.0, 0.0, None
            return (
                round(100.0 * int(row[1]) / total, 4),
                round(100.0 * int(row[2]) / total, 4),
                None,
            )
        else:
            row = con.execute(f"""
                SELECT
                  count(*) AS total,
                  count(*) FILTER (WHERE individual_crd IS NULL) AS id_null,
                  count(*) FILTER (WHERE individual_first_normalized IS NULL
                                     AND individual_last_normalized IS NULL) AS name_null,
                  count(*) FILTER (WHERE current_employer_crd IS NULL) AS emp_null
                FROM read_parquet('{final_path}');
            """).fetchone()
            total = int(row[0]) if row else 0
            if total == 0:
                return 0.0, 0.0, 0.0
            return (
                round(100.0 * int(row[1]) / total, 4),
                round(100.0 * int(row[2]) / total, 4),
                round(100.0 * int(row[3]) / total, 4),
            )
    finally:
        con.close()


def run_one_kind(
    spec: KindSpec, *,
    snapshot_date: date,
    rate: RateLimiter, rate_limit_rps: float,
    seed_prefixes: list[str],
    workdir: Path, batch_rows: int,
    max_crds: int | None,
    r2_prefix_override: str | None,
    skip_enumerate: bool,
    skip_already_fetched: bool,
    dry_run: bool,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== INGEST: kind=%s snapshot=%s ===", spec.name, snapshot_date.isoformat())
    log.info("=" * 70)

    enumeration_strategy = (
        f"recursive-prefix:{''.join(seed_prefixes)};drilldown=[a-z0-9 ];"
        f"depth_cap={DRILLDOWN_DEPTH_CAP}"
    )
    schema = _FIRM_SCHEMA if spec.name == "firms" else _INDIVIDUAL_SCHEMA
    project_fn = (
        project_firm_record if spec.name == "firms"
        else project_individual_record
    )

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    http_stats = HttpStats()

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        if dry_run:
            log.info("[%s] DRY RUN — probing 3 prefixes only, no DB/R2 writes",
                     spec.name)
            for p in seed_prefixes[:3]:
                t = _search_total(client, spec, p, rate=rate, stats=http_stats)
                log.info("dry-run [%s] prefix=%r total=%s", spec.name, p, t)
            return 0

        with psycopg.connect(_database_url()) as conn:
            run_id = insert_run_row(
                conn,
                snapshot_date=snapshot_date, table_name=spec.name,
                enumeration_strategy=enumeration_strategy,
                rate_limit_rps=rate_limit_rps,
            )
            log.info("[%s] run id=%s", spec.name, run_id)

            stage_dir = _stage_dir(workdir, spec.name)
            final_path = _final_parquet_path(workdir, spec.name)

            try:
                # ---- Phase 1: enumerate ----
                drilled = 0
                if skip_enumerate:
                    crds = load_crds(workdir, spec.name)
                    if not crds:
                        log.warning("[%s] --skip-enumerate but no saved CRDs; "
                                    "running enumeration after all", spec.name)
                        crds, drilled = enumerate_crds(
                            client, spec, seed_prefixes=seed_prefixes,
                            rate=rate, stats=http_stats,
                        )
                        save_crds(workdir, spec.name, crds)
                else:
                    crds, drilled = enumerate_crds(
                        client, spec, seed_prefixes=seed_prefixes,
                        rate=rate, stats=http_stats,
                    )
                    save_crds(workdir, spec.name, crds)
                log.info("[%s] enumeration done: %d unique CRDs, %d prefixes drilled",
                         spec.name, len(crds), drilled)

                if max_crds is not None:
                    crds = crds[: max_crds]
                    log.info("[%s] truncated to first %d CRDs (--max-crds)",
                             spec.name, len(crds))

                # ---- Phase 2: detail fetch ----
                detail_stats = detail_fetch_loop(
                    client, spec, crds,
                    rate=rate, http_stats=http_stats,
                    schema=schema,
                    project_fn=project_fn,
                    snapshot_date=snapshot_date,
                    stage_dir=stage_dir,
                    batch_rows=batch_rows,
                    skip_already_fetched=skip_already_fetched,
                )
                log.info("[%s] detail-fetch: total=%d success=%d failed=%d "
                         "parts_written=%d",
                         spec.name, detail_stats.total, detail_stats.success,
                         detail_stats.failed, detail_stats.parts_written)

                # ---- Concat staging → final → R2 ----
                rows_pq, bytes_pq = concat_staging_to_final(
                    stage_dir, final_path, schema=schema, kind=spec.name,
                )
                log.info("[%s] final parquet: %d rows, %.1f MB",
                         spec.name, rows_pq, bytes_pq / (1 << 20))

                target_prefix = r2_prefix_override or (
                    f"finra-brokercheck/snapshot={snapshot_date.isoformat()}"
                )
                target_key = (
                    target_prefix.rstrip("/") + f"/{spec.name}.parquet"
                )
                uploaded = upload_to_r2(final_path, key=target_key)
                log.info("[%s] uploaded → s3://%s/%s (%.1f MB)",
                         spec.name, R2_BUCKET, target_key, uploaded / (1 << 20))

                # ---- Compute null-rate sanity ----
                pk_null, name_null, emp_null = compute_null_rates(final_path, spec.name)
                log.info(
                    "[%s] null-rate primary_id=%.2f%% primary_name=%.2f%% "
                    "current_employer_crd=%s",
                    spec.name, pk_null, name_null,
                    f"{emp_null:.2f}%" if emp_null is not None else "n/a",
                )

                # ---- Finalize audit row ----
                finalize_run_row(
                    conn, run_id, status="completed",
                    started_wall=started_wall,
                    unique_crds=len(crds),
                    search_calls_total=http_stats.total - detail_stats.total,
                    search_calls_4xx=None,
                    search_calls_5xx=None,
                    prefixes_drilled=drilled,
                    detail_calls_total=detail_stats.total,
                    detail_calls_success=detail_stats.success,
                    detail_calls_failed=detail_stats.failed,
                    detail_calls_4xx=detail_stats.http_4xx,
                    detail_calls_5xx=detail_stats.http_5xx,
                    parquet_row_count=rows_pq,
                    parquet_bytes_written=bytes_pq,
                    parquet_column_count=len(schema),
                    r2_bucket=R2_BUCKET,
                    r2_prefix=target_prefix.rstrip("/") + "/",
                    r2_object_key=target_key,
                    r2_total_bytes=uploaded,
                    primary_id_null_pct=pk_null,
                    primary_name_null_pct=name_null,
                    current_employer_crd_null_pct=emp_null,
                    error_message=None,
                    notes={
                        "max_crds": max_crds,
                        "r2_prefix_override": r2_prefix_override,
                        "skip_enumerate": skip_enumerate,
                        "skip_already_fetched": skip_already_fetched,
                        "parts_written_this_run": detail_stats.parts_written,
                    },
                )
                log.info("[%s] DONE wall=%.1fs",
                         spec.name, time.monotonic() - started_wall)
                return 0

            except Exception as exc:
                log.exception("[%s] failed", spec.name)
                try:
                    finalize_run_row(
                        conn, run_id, status="failed",
                        started_wall=started_wall,
                        unique_crds=None,
                        search_calls_total=http_stats.total,
                        search_calls_4xx=http_stats.by_4xx,
                        search_calls_5xx=http_stats.by_5xx,
                        prefixes_drilled=None,
                        detail_calls_total=None,
                        detail_calls_success=None,
                        detail_calls_failed=None,
                        detail_calls_4xx=None,
                        detail_calls_5xx=None,
                        parquet_row_count=None,
                        parquet_bytes_written=None,
                        parquet_column_count=None,
                        r2_bucket=None,
                        r2_prefix=None,
                        r2_object_key=None,
                        r2_total_bytes=None,
                        primary_id_null_pct=None,
                        primary_name_null_pct=None,
                        current_employer_crd_null_pct=None,
                        error_message=str(exc),
                        notes=None,
                    )
                except Exception:
                    log.exception("[%s] failed to finalize audit row on error",
                                  spec.name)
                return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=["firms", "individuals", "both"],
                   default="both",
                   help="Which kind to ingest (default: both, firms first).")
    p.add_argument("--rate-limit-rps", type=float,
                   default=DEFAULT_RATE_LIMIT_RPS,
                   help=f"Max requests per second. Default {DEFAULT_RATE_LIMIT_RPS}.")
    p.add_argument("--prefix", default=None,
                   help="Comma-separated seed prefixes (smoke testing). "
                        "Default: a-z + 0-9.")
    p.add_argument("--max-crds", type=int, default=None,
                   help="Smoke testing: stop after fetching N CRDs per kind.")
    p.add_argument("--workdir", default=None,
                   help="Staging directory. Default /tmp/finra_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical finra-brokercheck/snapshot=YYYY-MM-DD/ "
                        "prefix (smoke testing).")
    p.add_argument("--snapshot-date", default=None,
                   help="ISO date (YYYY-MM-DD) for the snapshot partition. "
                        "Default: today UTC.")
    p.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS,
                   help="Detail-fetch flush every N records. "
                        f"Default {DEFAULT_BATCH_ROWS}.")
    p.add_argument("--skip-enumerate", action="store_true",
                   help="Resume mode: load enumerated CRDs from disk instead "
                        "of re-walking the prefix tree.")
    p.add_argument("--skip-already-fetched", action="store_true",
                   help="Resume mode: skip CRDs whose detail rows are already "
                        "present in staging Parquet parts. Implies "
                        "--skip-enumerate makes sense.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD/probe only; no DB or R2 writes.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.snapshot_date:
        snapshot = datetime.strptime(args.snapshot_date, "%Y-%m-%d").date()
    else:
        snapshot = datetime.now(timezone.utc).date()

    workdir = Path(args.workdir or "/tmp/finra_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    seed_prefixes = (
        [p.strip() for p in args.prefix.split(",") if p.strip()]
        if args.prefix else list(DEFAULT_SEED_PREFIX_ALPHABET)
    )

    rate = RateLimiter(args.rate_limit_rps)

    if args.kind == "both":
        kinds: list[KindSpec] = [FIRM_SPEC, INDIVIDUAL_SPEC]
    elif args.kind == "firms":
        kinds = [FIRM_SPEC]
    else:
        kinds = [INDIVIDUAL_SPEC]

    rc = 0
    for spec in kinds:
        rc_one = run_one_kind(
            spec,
            snapshot_date=snapshot,
            rate=rate, rate_limit_rps=args.rate_limit_rps,
            seed_prefixes=seed_prefixes,
            workdir=workdir, batch_rows=args.batch_rows,
            max_crds=args.max_crds,
            r2_prefix_override=args.r2_prefix_override,
            skip_enumerate=args.skip_enumerate,
            skip_already_fetched=args.skip_already_fetched,
            dry_run=args.dry_run,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("[%s] failed; continuing with remaining kinds if any",
                      spec.name)

    return rc


if __name__ == "__main__":
    sys.exit(main())
