"""Async HTTP client for Exa's research API.

Thin wrapper, one method per supported endpoint. The client returns
the parsed JSON response unchanged plus a ``_meta`` envelope with
duration / Exa request id / cost — the Trigger.dev worker uses
``_meta`` to populate the audit columns on ``exa.exa_calls`` without
re-parsing the raw body.

Auth: ``x-api-key: <EXA_API_KEY>`` per Exa docs (Bearer also accepted
upstream; we standardize on x-api-key).

The ``research`` endpoint is poll-based: ``POST /research/v1`` returns
a ``researchId`` immediately and ``GET /research/v1/{id}`` returns the
final result when ``status == 'completed'``. We poll internally so
callers see one sync-style call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ExaError(Exception):
    pass


class ExaNotConfiguredError(ExaError):
    pass


class ExaCallError(ExaError):
    def __init__(self, *, status_code: int, body: str, endpoint: str) -> None:
        super().__init__(
            f"Exa {endpoint} returned HTTP {status_code}: {body[:500]}"
        )
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint


# Short-endpoint timeout. Search / contents / findSimilar / answer are
# typically sub-30s; 60s gives Exa-side variance plus our network slack.
_DEFAULT_TIMEOUT = 60.0
# Research polling: cap the whole orchestration at 10 minutes (matches the
# Trigger.dev task maxDuration headroom) and poll every 5s.
_RESEARCH_TIMEOUT = 600.0
_RESEARCH_POLL_INTERVAL = 5.0


def _api_key_or_raise() -> str:
    secret = settings.EXA_API_KEY
    if not secret:
        raise ExaNotConfiguredError(
            "EXA_API_KEY is not configured — cannot call Exa"
        )
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    return str(secret)


def _headers() -> dict[str, str]:
    return {
        "x-api-key": _api_key_or_raise(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base_url() -> str:
    return (settings.EXA_API_BASE or "https://api.exa.ai").rstrip("/")


def _extract_meta(
    *,
    started_at: float,
    finished_at: float,
    response_headers: httpx.Headers | None,
    response_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pull observability fields out of Exa's response. Headers/body
    field names are best-effort — if Exa changes them we still return
    duration_ms, just with nulls for the rest.
    """
    duration_ms = int((finished_at - started_at) * 1000)
    request_id: str | None = None
    cost: float | None = None
    if response_headers is not None:
        # Common Exa observability headers; fall through to body if absent.
        for header_key in ("x-exa-request-id", "x-request-id", "exa-request-id"):
            if header_key in response_headers:
                request_id = response_headers[header_key]
                break
    if response_json:
        if request_id is None:
            for body_key in ("requestId", "request_id"):
                v = response_json.get(body_key)
                if isinstance(v, str):
                    request_id = v
                    break
        cost_raw = (
            response_json.get("costDollars")
            or response_json.get("cost_dollars")
            or response_json.get("cost")
        )
        if isinstance(cost_raw, (int, float)):
            cost = float(cost_raw)
        elif isinstance(cost_raw, dict):
            total = cost_raw.get("total")
            if isinstance(total, (int, float)):
                cost = float(total)
    return {
        "duration_ms": duration_ms,
        "exa_request_id": request_id,
        "cost_dollars": cost,
    }


async def _post(
    path: str,
    body: dict[str, Any],
    *,
    timeout: float,
    endpoint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST helper. Returns (response_json, meta). Raises on non-2xx."""
    url = f"{_base_url()}{path}"
    loop = asyncio.get_event_loop()
    started = loop.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=_headers())
    finished = loop.time()
    text = resp.text
    if resp.status_code >= 400:
        raise ExaCallError(
            status_code=resp.status_code, body=text, endpoint=endpoint
        )
    payload = resp.json() if text else {}
    meta = _extract_meta(
        started_at=started,
        finished_at=finished,
        response_headers=resp.headers,
        response_json=payload if isinstance(payload, dict) else None,
    )
    return payload, meta


async def _get(
    path: str,
    *,
    timeout: float,
    endpoint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"{_base_url()}{path}"
    loop = asyncio.get_event_loop()
    started = loop.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_headers())
    finished = loop.time()
    text = resp.text
    if resp.status_code >= 400:
        raise ExaCallError(
            status_code=resp.status_code, body=text, endpoint=endpoint
        )
    payload = resp.json() if text else {}
    meta = _extract_meta(
        started_at=started,
        finished_at=finished,
        response_headers=resp.headers,
        response_json=payload if isinstance(payload, dict) else None,
    )
    return payload, meta


# ---------------------------------------------------------------------------
# Public surface — one method per supported endpoint.
# ---------------------------------------------------------------------------


async def search(
    *,
    query: str,
    num_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """POST /search. Returns the parsed Exa response with a ``_meta`` key
    appended (duration_ms / exa_request_id / cost_dollars)."""
    body: dict[str, Any] = {"query": query, "numResults": num_results, **kwargs}
    payload, meta = await _post(
        "/search", body, timeout=_DEFAULT_TIMEOUT, endpoint="search"
    )
    payload["_meta"] = meta
    return payload


async def contents(
    *,
    urls: list[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """POST /contents. URLs is the canonical input field per the API."""
    body: dict[str, Any] = {"urls": urls, **kwargs}
    payload, meta = await _post(
        "/contents", body, timeout=_DEFAULT_TIMEOUT, endpoint="contents"
    )
    payload["_meta"] = meta
    return payload


async def find_similar(
    *,
    url: str,
    num_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """POST /findSimilar. Exa uses camelCase ``findSimilar`` for the path."""
    body: dict[str, Any] = {"url": url, "numResults": num_results, **kwargs}
    payload, meta = await _post(
        "/findSimilar", body, timeout=_DEFAULT_TIMEOUT, endpoint="find_similar"
    )
    payload["_meta"] = meta
    return payload


async def answer(
    *,
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """POST /answer."""
    body: dict[str, Any] = {"query": query, **kwargs}
    payload, meta = await _post(
        "/answer", body, timeout=_DEFAULT_TIMEOUT, endpoint="answer"
    )
    payload["_meta"] = meta
    return payload


async def research(
    *,
    instructions: str,
    poll_interval: float = _RESEARCH_POLL_INTERVAL,
    overall_timeout: float = _RESEARCH_TIMEOUT,
    **kwargs: Any,
) -> dict[str, Any]:
    """POST /research/v1, then poll GET /research/v1/{id} until terminal.

    Returns the final ``GET`` payload with ``_meta`` reflecting the
    full sync-style call duration + the most recent request id. If the
    task does not reach a terminal state inside ``overall_timeout`` we
    raise ``ExaCallError`` so the job row transitions to failed cleanly
    rather than hanging the Trigger run.
    """
    body: dict[str, Any] = {"instructions": instructions, **kwargs}
    create_payload, _create_meta = await _post(
        "/research/v1",
        body,
        timeout=_DEFAULT_TIMEOUT,
        endpoint="research",
    )
    research_id = (
        create_payload.get("researchId")
        or create_payload.get("id")
        or create_payload.get("research_id")
    )
    if not research_id:
        raise ExaCallError(
            status_code=502,
            body=f"Exa /research/v1 did not return a researchId: {create_payload!r}",
            endpoint="research",
        )

    loop = asyncio.get_event_loop()
    started = loop.time()
    deadline = started + overall_timeout
    last_payload: dict[str, Any] = {}
    last_headers: httpx.Headers | None = None
    while loop.time() < deadline:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                f"{_base_url()}/research/v1/{research_id}",
                headers=_headers(),
            )
        if resp.status_code >= 400:
            raise ExaCallError(
                status_code=resp.status_code,
                body=resp.text,
                endpoint="research",
            )
        last_payload = resp.json() if resp.text else {}
        last_headers = resp.headers
        status = (last_payload.get("status") or "").lower()
        if status in ("completed", "succeeded", "success"):
            finished = loop.time()
            meta = _extract_meta(
                started_at=started,
                finished_at=finished,
                response_headers=last_headers,
                response_json=last_payload,
            )
            last_payload["_meta"] = meta
            last_payload["_research_id"] = research_id
            return last_payload
        if status in ("failed", "error", "cancelled", "canceled"):
            raise ExaCallError(
                status_code=502,
                body=f"Exa research task terminated as {status}: {last_payload!r}",
                endpoint="research",
            )
        await asyncio.sleep(poll_interval)

    raise ExaCallError(
        status_code=504,
        body=(
            f"Exa research task {research_id} did not complete within "
            f"{overall_timeout:.0f}s; last payload: {last_payload!r}"
        ),
        endpoint="research",
    )


__all__ = [
    "ExaError",
    "ExaNotConfiguredError",
    "ExaCallError",
    "search",
    "contents",
    "find_similar",
    "answer",
    "research",
]
