"""Dub.co HTTP client.

Hand-rolled httpx wrapper around the Dub API. Single-tenant: every function
takes an explicit `api_key` (the global DUB_API_KEY); no per-org credential
plumbing. Mirrors the Lob client at `app/providers/lob/client.py` for retry
semantics, error categorization, and call-site grep-ability.

Snake↔camel translation happens at the client boundary via an explicit
allowlist (`_LINK_FIELD_TO_CAMEL`, `_ANALYTICS_FIELD_TO_CAMEL`,
`_EVENT_FIELD_TO_CAMEL`) — the rest of the codebase stays snake_case. We
deliberately do not use a recursive transformer.
"""

from __future__ import annotations

import random
import time
from typing import Any, Literal

import httpx

DUB_API_BASE = "https://api.dub.co"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0

_EP_LINKS = "/links"
_EP_LINKS_INFO = "/links/info"
_EP_LINKS_BULK = "/links/bulk"
_EP_ANALYTICS = "/analytics"
_EP_EVENTS = "/events"


# Snake_case → camelCase allowlists. New Dub fields must be added here
# explicitly; we'd rather forget a field and have the test suite fail than
# silently pass through unknown keys with a generic transformer.
_LINK_FIELD_TO_CAMEL = {
    "url": "url",
    "domain": "domain",
    "key": "key",
    "external_id": "externalId",
    "tenant_id": "tenantId",
    "tag_ids": "tagIds",
    "tag_names": "tagNames",
    "comments": "comments",
    "track_conversion": "trackConversion",
    "expires_at": "expiresAt",
    "expired_url": "expiredUrl",
    "ios": "ios",
    "android": "android",
    "geo": "geo",
    "archived": "archived",
    "rewrite": "rewrite",
    "do_index": "doIndex",
    "title": "title",
    "description": "description",
    "image": "image",
    "video": "video",
    "password": "password",
    "proxy": "proxy",
    "public_stats": "publicStats",
    "utm_source": "utm_source",
    "utm_medium": "utm_medium",
    "utm_campaign": "utm_campaign",
    "utm_term": "utm_term",
    "utm_content": "utm_content",
}

_LINK_LIST_PARAM_TO_CAMEL = {
    "tenant_id": "tenantId",
    "tag_ids": "tagIds",
    "search": "search",
    "domain": "domain",
    "page": "page",
    "page_size": "pageSize",
    "sort_by": "sortBy",
    "sort_order": "sortOrder",
}

_ANALYTICS_PARAM_TO_CAMEL = {
    "event": "event",
    "group_by": "groupBy",
    "interval": "interval",
    "start": "start",
    "end": "end",
    "link_id": "linkId",
    "external_id": "externalId",
    "tenant_id": "tenantId",
    "domain": "domain",
    "key": "key",
    "tag_ids": "tagIds",
}

_EVENT_PARAM_TO_CAMEL = {
    "event": "event",
    "interval": "interval",
    "start": "start",
    "end": "end",
    "link_id": "linkId",
    "external_id": "externalId",
    "tenant_id": "tenantId",
    "page": "page",
}


class DubProviderError(Exception):
    """Raised on any non-2xx Dub response or transport failure.

    Carries the upstream error code from the {error: {code, message, doc_url}}
    envelope when present so callers can branch on it.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        doc_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.doc_url = doc_url

    @property
    def category(self) -> str:
        if self.status in _RETRYABLE_STATUS_CODES:
            return "transient"
        if self.status in (400, 401, 403, 404, 409, 422):
            return "terminal"
        return "unknown"

    @property
    def retryable(self) -> bool:
        return self.category == "transient"


def _build_base_url(base_url: str | None) -> str:
    return (base_url or DUB_API_BASE).rstrip("/")


def _build_auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _camel_payload(snake: dict[str, Any], allowlist: dict[str, str]) -> dict[str, Any]:
    """Project a snake_case dict onto its Dub camelCase counterpart.

    Drops keys whose value is None, and drops keys not in the allowlist
    (rather than silently passing them through). Caller is responsible for
    handing us only fields they want sent.
    """
    out: dict[str, Any] = {}
    for k, v in snake.items():
        if v is None:
            continue
        camel = allowlist.get(k)
        if camel is None:
            continue
        out[camel] = v
    return out


def _request_with_retry(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> httpx.Response:
    last_exc: httpx.HTTPError | None = None
    response: httpx.Response | None = None
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_payload,
                )
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= _MAX_RETRY_ATTEMPTS:
                raise
            delay = min(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_DELAY_SECONDS)
            delay += random.uniform(0, delay * 0.2)
            time.sleep(delay)
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRY_ATTEMPTS:
            delay = min(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_DELAY_SECONDS)
            delay += random.uniform(0, delay * 0.2)
            time.sleep(delay)
            continue
        return response

    if last_exc:
        raise last_exc
    assert response is not None
    return response


def _raise_from_response(response: httpx.Response) -> None:
    status = response.status_code
    code: str | None = None
    doc_url: str | None = None
    message: str
    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or f"HTTP {status}"
            doc_url = err.get("doc_url")
        elif isinstance(err, str):
            message = err
        else:
            message = body.get("message") or f"HTTP {status}"
    else:
        message = f"HTTP {status}: {response.text[:200]}"

    raise DubProviderError(message, status=status, code=code, doc_url=doc_url)


def _request_json(
    *,
    method: str,
    path: str,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> Any:
    if not api_key:
        raise DubProviderError("Missing Dub API key", status=None)

    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    request_headers.update(_build_auth_headers(api_key))

    url = f"{_build_base_url(base_url)}{path}"
    try:
        response = _request_with_retry(
            method=method,
            url=url,
            headers=request_headers,
            timeout_seconds=timeout_seconds,
            params=params or None,
            json_payload=json_payload,
        )
    except httpx.HTTPError as exc:
        raise DubProviderError(f"Dub connectivity error: {exc}", status=None) from exc

    if response.status_code == 204:
        return None
    if response.status_code >= 400:
        _raise_from_response(response)

    try:
        return response.json()
    except ValueError as exc:
        raise DubProviderError(
            "Dub returned non-JSON response", status=response.status_code
        ) from exc


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def create_link(
    *,
    api_key: str,
    url: str,
    domain: str | None = None,
    key: str | None = None,
    external_id: str | None = None,
    tenant_id: str | None = None,
    tag_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    comments: str | None = None,
    track_conversion: bool | None = None,
    expires_at: str | None = None,
    expired_url: str | None = None,
    ios: str | None = None,
    android: str | None = None,
    geo: dict[str, str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """POST /links. Returns the dub link object verbatim."""
    snake = {
        "url": url,
        "domain": domain,
        "key": key,
        "external_id": external_id,
        "tenant_id": tenant_id,
        "tag_ids": tag_ids,
        "tag_names": tag_names,
        "comments": comments,
        "track_conversion": track_conversion,
        "expires_at": expires_at,
        "expired_url": expired_url,
        "ios": ios,
        "android": android,
        "geo": geo,
    }
    payload = _camel_payload(snake, _LINK_FIELD_TO_CAMEL)
    data = _request_json(
        method="POST",
        path=_EP_LINKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub create link response type")
    return data


def get_link(
    *,
    api_key: str,
    link_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """GET /links/{linkId}. linkId can be the dub id ('link_…') or 'ext_…'."""
    data = _request_json(
        method="GET",
        path=f"{_EP_LINKS}/{link_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub get link response type")
    return data


def get_link_by_external_id(
    *,
    api_key: str,
    external_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """GET /links/info?externalId=… — preferred when you only have your own ID."""
    data = _request_json(
        method="GET",
        path=_EP_LINKS_INFO,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params={"externalId": external_id},
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub get link info response type")
    return data


def list_links(
    *,
    api_key: str,
    tenant_id: str | None = None,
    tag_ids: list[str] | None = None,
    search: str | None = None,
    domain: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """GET /links. Returns list of link objects."""
    snake = {
        "tenant_id": tenant_id,
        "tag_ids": tag_ids,
        "search": search,
        "domain": domain,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    params = _camel_payload(snake, _LINK_LIST_PARAM_TO_CAMEL)
    data = _request_json(
        method="GET",
        path=_EP_LINKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params or None,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list links response type")
    return data


def update_link(
    *,
    api_key: str,
    link_id: str,
    fields: dict[str, Any],
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """PATCH /links/{linkId}. `fields` is the partial body in snake_case."""
    payload = _camel_payload(fields, _LINK_FIELD_TO_CAMEL)
    data = _request_json(
        method="PATCH",
        path=f"{_EP_LINKS}/{link_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub update link response type")
    return data


def delete_link(
    *,
    api_key: str,
    link_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> None:
    """DELETE /links/{linkId}. Use sparingly; prefer archiving."""
    _request_json(
        method="DELETE",
        path=f"{_EP_LINKS}/{link_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Analytics + events
# ---------------------------------------------------------------------------


def retrieve_analytics(
    *,
    api_key: str,
    event: Literal["clicks", "leads", "sales", "composite"] = "clicks",
    group_by: str = "count",
    interval: str | None = None,
    start: str | None = None,
    end: str | None = None,
    link_id: str | None = None,
    external_id: str | None = None,
    tenant_id: str | None = None,
    domain: str | None = None,
    key: str | None = None,
    tag_ids: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> Any:
    """GET /analytics. Return shape varies by group_by — pass through verbatim."""
    snake = {
        "event": event,
        "group_by": group_by,
        "interval": interval,
        "start": start,
        "end": end,
        "link_id": link_id,
        "external_id": external_id,
        "tenant_id": tenant_id,
        "domain": domain,
        "key": key,
        "tag_ids": tag_ids,
    }
    params = _camel_payload(snake, _ANALYTICS_PARAM_TO_CAMEL)
    return _request_json(
        method="GET",
        path=_EP_ANALYTICS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )


def list_events(
    *,
    api_key: str,
    event: Literal["clicks", "leads", "sales"] = "clicks",
    interval: str | None = None,
    start: str | None = None,
    end: str | None = None,
    link_id: str | None = None,
    external_id: str | None = None,
    tenant_id: str | None = None,
    page: int | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """GET /events. Raw event log (clicks, leads, sales)."""
    snake = {
        "event": event,
        "interval": interval,
        "start": start,
        "end": end,
        "link_id": link_id,
        "external_id": external_id,
        "tenant_id": tenant_id,
        "page": page,
    }
    params = _camel_payload(snake, _EVENT_PARAM_TO_CAMEL)
    data = _request_json(
        method="GET",
        path=_EP_EVENTS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list events response type")
    return data
