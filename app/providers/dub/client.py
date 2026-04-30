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
_EP_FOLDERS = "/folders"
_EP_TAGS = "/tags"
_EP_WEBHOOKS = "/webhooks"
_EP_TRACK_LEAD = "/track/lead"
_EP_TRACK_SALE = "/track/sale"
_EP_DOMAINS = "/domains"

_BULK_LINK_MAX = 100


# Snake_case → camelCase allowlists. New Dub fields must be added here
# explicitly; we'd rather forget a field and have the test suite fail than
# silently pass through unknown keys with a generic transformer.
_LINK_FIELD_TO_CAMEL = {
    "url": "url",
    "domain": "domain",
    "key": "key",
    "external_id": "externalId",
    "tenant_id": "tenantId",
    "folder_id": "folderId",
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
    "country": "country",
    "city": "city",
    "region": "region",
    "continent": "continent",
    "device": "device",
    "browser": "browser",
    "os": "os",
    "referer": "referer",
    "referer_url": "refererUrl",
    "url": "url",
    "qr": "qr",
    "trigger": "trigger",
    "folder_id": "folderId",
    "customer_id": "customerId",
    "timezone": "timezone",
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
    "domain": "domain",
    "key": "key",
    "tag_ids": "tagIds",
    "country": "country",
    "city": "city",
    "region": "region",
    "continent": "continent",
    "device": "device",
    "browser": "browser",
    "os": "os",
    "referer": "referer",
    "referer_url": "refererUrl",
    "url": "url",
    "qr": "qr",
    "trigger": "trigger",
    "folder_id": "folderId",
    "customer_id": "customerId",
    "timezone": "timezone",
}

_FOLDER_FIELD_TO_CAMEL = {
    "name": "name",
    "access_level": "accessLevel",
}

_TAG_FIELD_TO_CAMEL = {
    "name": "name",
    "color": "color",
}

_WEBHOOK_FIELD_TO_CAMEL = {
    "name": "name",
    "url": "url",
    "secret": "secret",
    "triggers": "triggers",
    "link_ids": "linkIds",
    "tag_ids": "tagIds",
    "disabled": "disabled",
}

_TRACK_LEAD_FIELD_TO_CAMEL = {
    "click_id": "clickId",
    "event_name": "eventName",
    "customer_external_id": "customerExternalId",
    "customer_name": "customerName",
    "customer_email": "customerEmail",
    "customer_avatar": "customerAvatar",
    "metadata": "metadata",
}

_TRACK_SALE_FIELD_TO_CAMEL = {
    "customer_external_id": "customerExternalId",
    "amount": "amount",
    "currency": "currency",
    "event_name": "eventName",
    "invoice_id": "invoiceId",
    "payment_processor": "paymentProcessor",
    "metadata": "metadata",
}

_DOMAIN_FIELD_TO_CAMEL = {
    "slug": "slug",
    "expired_url": "expiredUrl",
    "not_found_url": "notFoundUrl",
    "archived": "archived",
    "placeholder": "placeholder",
    "search": "search",
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
    folder_id: str | None = None,
    tag_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    comments: str | None = None,
    track_conversion: bool | None = None,
    expires_at: str | None = None,
    expired_url: str | None = None,
    ios: str | None = None,
    android: str | None = None,
    geo: dict[str, str] | None = None,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
    utm_term: str | None = None,
    utm_content: str | None = None,
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
        "folder_id": folder_id,
        "tag_ids": tag_ids,
        "tag_names": tag_names,
        "comments": comments,
        "track_conversion": track_conversion,
        "expires_at": expires_at,
        "expired_url": expired_url,
        "ios": ios,
        "android": android,
        "geo": geo,
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": utm_campaign,
        "utm_term": utm_term,
        "utm_content": utm_content,
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


_AnalyticsGroupBy = Literal[
    "count",
    "timeseries",
    "continents",
    "regions",
    "countries",
    "cities",
    "devices",
    "browsers",
    "os",
    "trigger",
    "referers",
    "referer_urls",
    "top_links",
    "top_urls",
    "top_domains",
    "top_link_tags",
    "top_folders",
    "utm_sources",
    "utm_mediums",
    "utm_campaigns",
    "utm_terms",
    "utm_contents",
]


def retrieve_analytics(
    *,
    api_key: str,
    event: Literal["clicks", "leads", "sales", "composite"] = "clicks",
    group_by: _AnalyticsGroupBy | str = "count",
    interval: str | None = None,
    start: str | None = None,
    end: str | None = None,
    link_id: str | None = None,
    external_id: str | None = None,
    tenant_id: str | None = None,
    domain: str | None = None,
    key: str | None = None,
    tag_ids: list[str] | None = None,
    country: str | None = None,
    city: str | None = None,
    region: str | None = None,
    continent: str | None = None,
    device: str | None = None,
    browser: str | None = None,
    os: str | None = None,
    referer: str | None = None,
    referer_url: str | None = None,
    url: str | None = None,
    qr: bool | None = None,
    trigger: str | None = None,
    folder_id: str | None = None,
    customer_id: str | None = None,
    timezone: str | None = None,
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
        "country": country,
        "city": city,
        "region": region,
        "continent": continent,
        "device": device,
        "browser": browser,
        "os": os,
        "referer": referer,
        "referer_url": referer_url,
        "url": url,
        "qr": qr,
        "trigger": trigger,
        "folder_id": folder_id,
        "customer_id": customer_id,
        "timezone": timezone,
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
    domain: str | None = None,
    key: str | None = None,
    tag_ids: list[str] | None = None,
    country: str | None = None,
    city: str | None = None,
    region: str | None = None,
    continent: str | None = None,
    device: str | None = None,
    browser: str | None = None,
    os: str | None = None,
    referer: str | None = None,
    referer_url: str | None = None,
    url: str | None = None,
    qr: bool | None = None,
    trigger: str | None = None,
    folder_id: str | None = None,
    customer_id: str | None = None,
    timezone: str | None = None,
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
        "domain": domain,
        "key": key,
        "tag_ids": tag_ids,
        "country": country,
        "city": city,
        "region": region,
        "continent": continent,
        "device": device,
        "browser": browser,
        "os": os,
        "referer": referer,
        "referer_url": referer_url,
        "url": url,
        "qr": qr,
        "trigger": trigger,
        "folder_id": folder_id,
        "customer_id": customer_id,
        "timezone": timezone,
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


# ---------------------------------------------------------------------------
# Bulk link operations
# ---------------------------------------------------------------------------


def _request_with_body(
    *,
    method: str,
    path: str,
    api_key: str,
    base_url: str | None,
    timeout_seconds: float,
    json_body: Any,
    params: dict[str, Any] | None = None,
) -> Any:
    """Variant of _request_json that sends a top-level JSON array (or anything
    non-dict) as the body. Mirrors _request_json otherwise.
    """
    if not api_key:
        raise DubProviderError("Missing Dub API key", status=None)

    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    request_headers.update(_build_auth_headers(api_key))
    url = f"{_build_base_url(base_url)}{path}"
    try:
        last_exc: httpx.HTTPError | None = None
        response: httpx.Response | None = None
        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=timeout_seconds) as client:
                    response = client.request(
                        method=method,
                        url=url,
                        headers=request_headers,
                        params=params or None,
                        json=json_body,
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= _MAX_RETRY_ATTEMPTS:
                    raise
                delay = min(
                    _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    _RETRY_MAX_DELAY_SECONDS,
                )
                delay += random.uniform(0, delay * 0.2)
                time.sleep(delay)
                continue

            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < _MAX_RETRY_ATTEMPTS
            ):
                delay = min(
                    _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    _RETRY_MAX_DELAY_SECONDS,
                )
                delay += random.uniform(0, delay * 0.2)
                time.sleep(delay)
                continue
            break

        if response is None:
            if last_exc:
                raise last_exc
            raise DubProviderError("no response from Dub")

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


def bulk_create_links(
    *,
    api_key: str,
    links: list[dict[str, Any]],
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """POST /links/bulk. Caller is responsible for chunking to ≤100 entries.

    Each entry is a snake_case dict matching create_link's kwargs (subset).
    Dub returns one result per input, in input order; failed entries surface
    as `{"error": {...}}` objects in the array. We do NOT raise on partial
    failure — caller decides how to handle per-entry errors.
    """
    if len(links) > _BULK_LINK_MAX:
        raise DubProviderError(
            f"bulk create exceeds {_BULK_LINK_MAX} links"
        )
    body = [_camel_payload(item, _LINK_FIELD_TO_CAMEL) for item in links]
    data = _request_with_body(
        method="POST",
        path=_EP_LINKS_BULK,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_body=body,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub bulk create response type")
    return data


def bulk_update_links(
    *,
    api_key: str,
    link_ids: list[str],
    fields: dict[str, Any],
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """PATCH /links/bulk. Apply the same partial update to many links."""
    body = {
        "linkIds": link_ids,
        "data": _camel_payload(fields, _LINK_FIELD_TO_CAMEL),
    }
    data = _request_with_body(
        method="PATCH",
        path=_EP_LINKS_BULK,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_body=body,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub bulk update response type")
    return data


def bulk_delete_links(
    *,
    api_key: str,
    link_ids: list[str],
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """DELETE /links/bulk?linkIds=…"""
    data = _request_json(
        method="DELETE",
        path=_EP_LINKS_BULK,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params={"linkIds": ",".join(link_ids)},
    )
    if data is None:
        return {"deletedCount": len(link_ids)}
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub bulk delete response type")
    return data


def upsert_link(
    *,
    api_key: str,
    url: str,
    domain: str | None = None,
    key: str | None = None,
    external_id: str | None = None,
    tenant_id: str | None = None,
    folder_id: str | None = None,
    tag_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """PUT /links — single link, keyed on externalId or domain+key."""
    snake = {
        "url": url,
        "domain": domain,
        "key": key,
        "external_id": external_id,
        "tenant_id": tenant_id,
        "folder_id": folder_id,
        "tag_ids": tag_ids,
        "tag_names": tag_names,
    }
    payload = _camel_payload(snake, _LINK_FIELD_TO_CAMEL)
    data = _request_json(
        method="PUT",
        path=_EP_LINKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub upsert link response type")
    return data


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def list_folders(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    data = _request_json(
        method="GET",
        path=_EP_FOLDERS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list folders response type")
    return data


def create_folder(
    *,
    api_key: str,
    name: str,
    access_level: str = "write",
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload(
        {"name": name, "access_level": access_level}, _FOLDER_FIELD_TO_CAMEL
    )
    data = _request_json(
        method="POST",
        path=_EP_FOLDERS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub create folder response type")
    return data


def get_folder(
    *,
    api_key: str,
    folder_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_FOLDERS}/{folder_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub get folder response type")
    return data


def update_folder(
    *,
    api_key: str,
    folder_id: str,
    name: str | None = None,
    access_level: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload(
        {"name": name, "access_level": access_level}, _FOLDER_FIELD_TO_CAMEL
    )
    data = _request_json(
        method="PATCH",
        path=f"{_EP_FOLDERS}/{folder_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub update folder response type")
    return data


def delete_folder(
    *,
    api_key: str,
    folder_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> None:
    _request_json(
        method="DELETE",
        path=f"{_EP_FOLDERS}/{folder_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def list_tags(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    data = _request_json(
        method="GET",
        path=_EP_TAGS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list tags response type")
    return data


def create_tag(
    *,
    api_key: str,
    name: str,
    color: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload({"name": name, "color": color}, _TAG_FIELD_TO_CAMEL)
    data = _request_json(
        method="POST",
        path=_EP_TAGS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub create tag response type")
    return data


def update_tag(
    *,
    api_key: str,
    tag_id: str,
    name: str | None = None,
    color: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload({"name": name, "color": color}, _TAG_FIELD_TO_CAMEL)
    data = _request_json(
        method="PATCH",
        path=f"{_EP_TAGS}/{tag_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub update tag response type")
    return data


def delete_tag(
    *,
    api_key: str,
    tag_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> None:
    _request_json(
        method="DELETE",
        path=f"{_EP_TAGS}/{tag_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Webhooks (CRUD against Dub)
# ---------------------------------------------------------------------------


def list_webhooks(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    data = _request_json(
        method="GET",
        path=_EP_WEBHOOKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list webhooks response type")
    return data


def create_webhook(
    *,
    api_key: str,
    name: str,
    url: str,
    triggers: list[str],
    secret: str | None = None,
    link_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload(
        {
            "name": name,
            "url": url,
            "triggers": triggers,
            "secret": secret,
            "link_ids": link_ids,
            "tag_ids": tag_ids,
        },
        _WEBHOOK_FIELD_TO_CAMEL,
    )
    data = _request_json(
        method="POST",
        path=_EP_WEBHOOKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub create webhook response type")
    return data


def get_webhook(
    *,
    api_key: str,
    webhook_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_WEBHOOKS}/{webhook_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub get webhook response type")
    return data


def update_webhook(
    *,
    api_key: str,
    webhook_id: str,
    name: str | None = None,
    url: str | None = None,
    triggers: list[str] | None = None,
    secret: str | None = None,
    link_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    disabled: bool | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    payload = _camel_payload(
        {
            "name": name,
            "url": url,
            "triggers": triggers,
            "secret": secret,
            "link_ids": link_ids,
            "tag_ids": tag_ids,
            "disabled": disabled,
        },
        _WEBHOOK_FIELD_TO_CAMEL,
    )
    data = _request_json(
        method="PATCH",
        path=f"{_EP_WEBHOOKS}/{webhook_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub update webhook response type")
    return data


def delete_webhook(
    *,
    api_key: str,
    webhook_id: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> None:
    _request_json(
        method="DELETE",
        path=f"{_EP_WEBHOOKS}/{webhook_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Conversion tracking (server-side)
# ---------------------------------------------------------------------------


def track_lead(
    *,
    api_key: str,
    click_id: str,
    event_name: str,
    customer_external_id: str,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_avatar: str | None = None,
    metadata: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """POST /track/lead. Records a conversion lead from a prior click.

    `click_id` is the `dub_id` cookie value captured on the landing page.
    `customer_external_id` is our internal recipients.id — Dub uses it as
    the customer key without us syncing a customers table.
    """
    payload = _camel_payload(
        {
            "click_id": click_id,
            "event_name": event_name,
            "customer_external_id": customer_external_id,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_avatar": customer_avatar,
            "metadata": metadata,
        },
        _TRACK_LEAD_FIELD_TO_CAMEL,
    )
    data = _request_json(
        method="POST",
        path=_EP_TRACK_LEAD,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub track lead response type")
    return data


def track_sale(
    *,
    api_key: str,
    customer_external_id: str,
    amount: int,
    currency: str = "usd",
    event_name: str = "Purchase",
    invoice_id: str | None = None,
    payment_processor: str | None = None,
    metadata: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """POST /track/sale. Records a sale (in cents) for an already-attributed
    customer (identified by customer_external_id from a prior track_lead)."""
    payload = _camel_payload(
        {
            "customer_external_id": customer_external_id,
            "amount": amount,
            "currency": currency,
            "event_name": event_name,
            "invoice_id": invoice_id,
            "payment_processor": payment_processor,
            "metadata": metadata,
        },
        _TRACK_SALE_FIELD_TO_CAMEL,
    )
    data = _request_json(
        method="POST",
        path=_EP_TRACK_SALE,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub track sale response type")
    return data


# ---------------------------------------------------------------------------
# Domains
#
# Dub workspaces serve `dub.sh` short links by default. Custom hosts
# (e.g. `track.acme.com`) need a `POST /domains` registration first; once
# that succeeds and the customer points DNS at Dub, links can be minted
# with `domain=track.acme.com`. The domain object id is what we persist on
# business.brands.dub_domain_config so we can DELETE it cleanly later.
# ---------------------------------------------------------------------------


def create_domain(
    *,
    api_key: str,
    slug: str,
    expired_url: str | None = None,
    not_found_url: str | None = None,
    archived: bool | None = None,
    placeholder: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """POST /domains. `slug` is the FQDN (e.g. 'track.acme.com')."""
    snake = {
        "slug": slug,
        "expired_url": expired_url,
        "not_found_url": not_found_url,
        "archived": archived,
        "placeholder": placeholder,
    }
    payload = _camel_payload(snake, _DOMAIN_FIELD_TO_CAMEL)
    data = _request_json(
        method="POST",
        path=_EP_DOMAINS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise DubProviderError("Unexpected Dub create domain response type")
    return data


def list_domains(
    *,
    api_key: str,
    archived: bool | None = None,
    search: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """GET /domains. Returns list of domain objects in the workspace."""
    snake = {"archived": archived, "search": search}
    params = _camel_payload(snake, _DOMAIN_FIELD_TO_CAMEL)
    data = _request_json(
        method="GET",
        path=_EP_DOMAINS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params or None,
    )
    if not isinstance(data, list):
        raise DubProviderError("Unexpected Dub list domains response type")
    return data


def get_domain_by_slug(
    *,
    api_key: str,
    slug: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any] | None:
    """Convenience: list domains and return the entry whose slug matches.

    Dub does not document a GET-by-slug, but list_domains is small (a
    workspace rarely has more than a handful of domains), so client-side
    filtering is fine.
    """
    rows = list_domains(api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)
    target = slug.lower().strip(".")
    for row in rows:
        if isinstance(row, dict):
            slug_val = row.get("slug")
            if isinstance(slug_val, str) and slug_val.lower() == target:
                return row
    return None


def delete_domain(
    *,
    api_key: str,
    slug: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> None:
    """DELETE /domains/{slug}."""
    _request_json(
        method="DELETE",
        path=f"{_EP_DOMAINS}/{slug}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
