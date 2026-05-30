"""SEC IAPD Form ADV Part 2 brochure HTTP client — async + rate-limited.

Wraps the two endpoints discovered in Stage 0 reconnaissance (see
~/Desktop/hq/directives/2026-05-10-sec-iapd-form-adv-part-2-brochure-scrape-phase-1.md):

  1. Manifest:
       GET https://api.adviserinfo.sec.gov/search/firm/{CRD}?hl=true&nrows=12&r=25&sort=score+desc&wt=json
       → JSON containing brochures.brochuredetails[]
         (brochureVersionID, brochureName, dateSubmitted)

  2. Download:
       GET https://files.adviserinfo.sec.gov/IAPD/Content/Common/crd_iapd_Brochure.aspx?BRCHR_VRSN_ID={version_id}
       → application/pdf (binary)

Both endpoints require Origin/Referer headers from adviserinfo.sec.gov; no
API-key auth. The directive's prior 19 curl probes failed because they omitted
Origin/Referer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

log = logging.getLogger("sec-iapd-brochure-client")

API_HOST = "https://api.adviserinfo.sec.gov"
FILES_HOST = "https://files.adviserinfo.sec.gov"
SPA_ORIGIN = "https://adviserinfo.sec.gov"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class BrochureManifestEntry:
    """One brochure version in a firm's IAPD manifest."""
    crd_number: int
    brochure_version_id: int
    brochure_name: str
    date_submitted: date
    brochure_type: str

    @property
    def source_url(self) -> str:
        return (
            f"{FILES_HOST}/IAPD/Content/Common/crd_iapd_Brochure.aspx"
            f"?BRCHR_VRSN_ID={self.brochure_version_id}"
        )

    @property
    def original_filename(self) -> str:
        return f"{self.brochure_version_id}.pdf"


@dataclass(frozen=True)
class FirmManifest:
    """Top-level manifest for one firm — what the scrape needs to know per CRD."""
    crd_number: int
    firm_name: str | None
    ia_scope: str | None
    is_sec_registered: str | None
    part2_exempt_flag: str | None
    brochures: list[BrochureManifestEntry]

    @property
    def is_scrapeable(self) -> bool:
        """Firm passes the directive's filter (active SEC-registered + Part 2 required)."""
        if self.ia_scope != "ACTIVE":
            return False
        if self.is_sec_registered != "Y":
            return False
        if (self.part2_exempt_flag or "").strip().upper() == "Y":
            return False
        return bool(self.brochures)


def _classify_brochure_type(name: str) -> str:
    """Infer brochure subtype from manifest name (no PDF parsing)."""
    upper = (name or "").upper()
    if "WRAP" in upper:
        return "Part2A_WrapFee"
    if "APPENDIX 1" in upper or "APPENDIX1" in upper:
        return "Part2A_WrapFee"
    if "PART 2B" in upper or "PART2B" in upper or "SUPPLEMENT" in upper:
        return "Part2B_Supplement"
    return "Part2A_FirmBrochure"


def _parse_filing_date(s: str) -> date | None:
    """IAPD ships dates as M/D/YYYY (no zero-padding); convert to ISO date."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    parts = s.split("/")
    if len(parts) == 3:
        try:
            m, d, y = (int(p) for p in parts)
            return date(y, m, d)
        except (ValueError, TypeError):
            return None
    return None


class RpsLimiter:
    """Token-bucket rate limiter shared across async workers."""
    def __init__(self, rps: float):
        self.rps = rps
        self.min_interval = 1.0 / rps
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class IapdBrochureClient:
    """Async client for IAPD's two-endpoint brochure surface."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RpsLimiter,
        *,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        timeout: float = 30.0,
    ):
        self._client = client
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._timeout = timeout

    async def _get(
        self, url: str, *, headers: dict[str, str], expect_binary: bool
    ) -> tuple[int, bytes, dict[str, str]]:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            await self._limiter.wait()
            try:
                r = await self._client.get(
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    follow_redirects=True,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = min(self._backoff_base ** attempt, 30)
                    log.warning(
                        "iapd HTTP %s on %s; retry %d/%d in %.1fs",
                        r.status_code, url, attempt, self._max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                return r.status_code, r.content, dict(r.headers)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_exc = exc
                wait = min(self._backoff_base ** attempt, 30)
                log.warning(
                    "iapd fetch %s threw %s; retry %d/%d in %.1fs",
                    url, exc, attempt, self._max_retries, wait,
                )
                await asyncio.sleep(wait)
        raise RuntimeError(f"iapd fetch {url} exhausted retries: {last_exc}")

    async def fetch_manifest(self, crd: int) -> FirmManifest | None:
        """Fetch the IAPD search/firm manifest for one CRD."""
        url = (
            f"{API_HOST}/search/firm/{int(crd)}"
            "?hl=true&nrows=12&r=25&sort=score+desc&wt=json"
        )
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": SPA_ORIGIN,
            "Referer": f"{SPA_ORIGIN}/firm/brochure/{int(crd)}",
        }
        status, body, _ = await self._get(url, headers=headers, expect_binary=False)
        if status != 200:
            log.warning("CRD %s manifest HTTP %s", crd, status)
            return None
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("CRD %s manifest JSON decode: %s", crd, exc)
            return None
        hits = envelope.get("hits", {}).get("hits", [])
        if not hits:
            return FirmManifest(
                crd_number=int(crd),
                firm_name=None,
                ia_scope=None,
                is_sec_registered=None,
                part2_exempt_flag=None,
                brochures=[],
            )
        try:
            inner_str = hits[0]["_source"]["iacontent"]
            inner: dict[str, Any] = json.loads(inner_str)
        except (KeyError, json.JSONDecodeError) as exc:
            log.warning("CRD %s manifest inner-JSON: %s", crd, exc)
            return None

        bi = inner.get("basicInformation", {}) or {}
        flags = inner.get("orgScopeStatusFlags", {}) or {}
        bro = inner.get("brochures", {}) or {}

        entries: list[BrochureManifestEntry] = []
        for raw in bro.get("brochuredetails", []) or []:
            try:
                vid = int(raw["brochureVersionID"])
            except (KeyError, TypeError, ValueError):
                continue
            name = raw.get("brochureName") or ""
            filed = _parse_filing_date(raw.get("dateSubmitted") or "")
            if filed is None:
                continue
            entries.append(BrochureManifestEntry(
                crd_number=int(crd),
                brochure_version_id=vid,
                brochure_name=name,
                date_submitted=filed,
                brochure_type=_classify_brochure_type(name),
            ))

        return FirmManifest(
            crd_number=int(crd),
            firm_name=bi.get("firmName"),
            ia_scope=bi.get("iaScope"),
            is_sec_registered=flags.get("isSECRegistered"),
            part2_exempt_flag=bro.get("part2ExemptFlag"),
            brochures=entries,
        )

    async def download_brochure(
        self, entry: BrochureManifestEntry
    ) -> tuple[bytes, str]:
        """Fetch one brochure PDF. Returns (bytes, content_type)."""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf, */*",
            "Referer": f"{SPA_ORIGIN}/firm/brochure/{entry.crd_number}",
        }
        status, body, resp_headers = await self._get(
            entry.source_url, headers=headers, expect_binary=True
        )
        if status != 200:
            raise RuntimeError(f"download HTTP {status} for {entry.source_url}")
        ctype = resp_headers.get("content-type", "application/pdf").split(";")[0].strip()
        return body, ctype
