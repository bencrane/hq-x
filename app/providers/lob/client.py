"""Lob HTTP client.

Hand-rolled httpx wrapper around the Lob API. Single-tenant: every function
takes an explicit `api_key` (the global LOB_API_KEY); no per-org credential
plumbing. Subset port from outbound-engine-x: checks, autocomplete, zip
lookup, reverse-geocode, identity, resource proofs, QR analytics, domains,
links, and billing groups are intentionally omitted — see ARCHITECTURE.md.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

LOB_API_BASE = "https://api.lob.com"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0

_EP_US_VERIFICATIONS = "/v1/us_verifications"
_EP_US_BULK_VERIFICATIONS = "/v1/bulk/us_verifications"
_EP_POSTCARDS = "/v1/postcards"
_EP_LETTERS = "/v1/letters"
_EP_SELF_MAILERS = "/v1/self_mailers"
_EP_SNAP_PACKS = "/v1/snap_packs"
_EP_BOOKLETS = "/v1/booklets"
_EP_TEMPLATES = "/v1/templates"
_EP_ADDRESSES = "/v1/addresses"
_EP_BUCKSLIPS = "/v1/buckslips"
_EP_CARDS = "/v1/cards"
_EP_CAMPAIGNS = "/v1/campaigns"
_EP_CREATIVES = "/v1/creatives"
_EP_UPLOADS = "/v1/uploads"


class LobProviderError(Exception):
    """Provider-level exception for Lob integration failures."""

    @property
    def category(self) -> str:
        message = str(self).lower()
        if (
            "connectivity error" in message
            or "http 429" in message
            or "http 500" in message
            or "http 502" in message
            or "http 503" in message
            or "http 504" in message
        ):
            return "transient"
        if (
            "invalid lob api key" in message
            or "endpoint not found" in message
            or "missing lob api key" in message
            or "cannot send both header and query idempotency keys" in message
            or "unexpected lob" in message
        ):
            return "terminal"
        return "unknown"

    @property
    def retryable(self) -> bool:
        return self.category == "transient"


def _build_base_url(base_url: str | None) -> str:
    return (base_url or LOB_API_BASE).rstrip("/")


def _build_basic_auth(api_key: str) -> tuple[str, str]:
    return (api_key, "")


def _request_with_retry(
    *,
    method: str,
    url: str,
    auth: tuple[str, str],
    headers: dict[str, str],
    timeout_seconds: float,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    files: Any | None = None,
) -> httpx.Response:
    last_exc: httpx.HTTPError | None = None
    response: httpx.Response | None = None
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.request(
                    method=method,
                    url=url,
                    auth=auth,
                    headers=headers,
                    params=params,
                    json=json_payload,
                    files=files,
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


def build_idempotency_material(
    *,
    header_key: str | None = None,
    query_key: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    if header_key and query_key:
        raise LobProviderError("Cannot send both header and query idempotency keys")

    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    if header_key:
        headers["Idempotency-Key"] = header_key
    if query_key:
        query["idempotency_key"] = query_key
    return headers, query


def _request_json(
    *,
    method: str,
    path: str,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
) -> Any:
    if not api_key:
        raise LobProviderError("Missing Lob API key")

    normalized_key = (
        idempotency_key.strip() if isinstance(idempotency_key, str) else idempotency_key
    )
    if normalized_key == "":
        raise LobProviderError("Idempotency key must be non-empty when provided")

    idempotency_headers, idempotency_query = build_idempotency_material(
        header_key=(normalized_key if normalized_key and not idempotency_in_query else None),
        query_key=(normalized_key if normalized_key and idempotency_in_query else None),
    )

    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    request_headers.update(idempotency_headers)

    request_params = dict(params or {})
    request_params.update(idempotency_query)

    url = f"{_build_base_url(base_url)}{path}"
    try:
        response = _request_with_retry(
            method=method,
            url=url,
            auth=_build_basic_auth(api_key),
            headers=request_headers,
            timeout_seconds=timeout_seconds,
            params=request_params or None,
            json_payload=json_payload,
        )
    except httpx.HTTPError as exc:
        raise LobProviderError(f"Lob connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise LobProviderError("Invalid Lob API key")
    if response.status_code == 404:
        raise LobProviderError(f"Lob endpoint not found: {path}")
    if response.status_code >= 400:
        raise LobProviderError(
            f"Lob API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LobProviderError("Lob returned non-JSON response") from exc


def _request_multipart(
    *,
    path: str,
    api_key: str,
    file_name: str,
    file_content: bytes,
    content_type: str = "text/csv",
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> Any:
    if not api_key:
        raise LobProviderError("Missing Lob API key")

    url = f"{_build_base_url(base_url)}{path}"
    try:
        response = _request_with_retry(
            method="POST",
            url=url,
            auth=_build_basic_auth(api_key),
            headers={"Accept": "application/json"},
            timeout_seconds=timeout_seconds,
            files={"file": (file_name, file_content, content_type)},
        )
    except httpx.HTTPError as exc:
        raise LobProviderError(f"Lob connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise LobProviderError("Invalid Lob API key")
    if response.status_code == 404:
        raise LobProviderError(f"Lob endpoint not found: {path}")
    if response.status_code >= 400:
        raise LobProviderError(
            f"Lob API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LobProviderError("Lob returned non-JSON response") from exc


def validate_api_key(
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 8.0,
) -> None:
    _request_json(
        method="GET",
        path=_EP_POSTCARDS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params={"limit": 1},
    )


# ---------------------------------------------------------------------------
# Address verification
# ---------------------------------------------------------------------------


def verify_address_us_single(
    api_key: str,
    payload: dict[str, Any],
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_US_VERIFICATIONS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob US verification response type")
    return data


def verify_address_us_bulk(
    api_key: str,
    payload: dict[str, Any],
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_US_BULK_VERIFICATIONS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob bulk US verification response type")
    return data


# ---------------------------------------------------------------------------
# Generic CRUD shape — every Lob piece type follows the same 4-call pattern
# (create / list / get / cancel-or-delete). We expose explicit named functions
# rather than a metaprogrammed dispatch so call sites stay grep-able.
# ---------------------------------------------------------------------------


def _piece_create(
    api_key: str,
    payload: dict[str, Any],
    *,
    endpoint: str,
    label: str,
    idempotency_key: str | None,
    idempotency_in_query: bool,
    base_url: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=endpoint,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError(f"Unexpected Lob create {label} response type")
    return data


def _piece_list(
    api_key: str,
    *,
    endpoint: str,
    label: str,
    params: dict[str, Any] | None,
    base_url: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=endpoint,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError(f"Unexpected Lob list {label} response type")
    return data


def _piece_get(
    api_key: str,
    piece_id: str,
    *,
    endpoint: str,
    label: str,
    base_url: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{endpoint}/{piece_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError(f"Unexpected Lob get {label} response type")
    return data


def _piece_cancel(
    api_key: str,
    piece_id: str,
    *,
    endpoint: str,
    label: str,
    idempotency_key: str | None,
    idempotency_in_query: bool,
    base_url: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{endpoint}/{piece_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError(f"Unexpected Lob cancel {label} response type")
    return data


# ---------------------------------------------------------------------------
# Postcards
# ---------------------------------------------------------------------------


def create_postcard(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_POSTCARDS,
        label="postcard",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_postcards(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_POSTCARDS,
        label="postcards",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_postcard(
    api_key: str,
    postcard_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        postcard_id,
        endpoint=_EP_POSTCARDS,
        label="postcard",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_postcard(
    api_key: str,
    postcard_id: str,
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_cancel(
        api_key,
        postcard_id,
        endpoint=_EP_POSTCARDS,
        label="postcard",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------


def create_letter(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_LETTERS,
        label="letter",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_letters(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_LETTERS,
        label="letters",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_letter(
    api_key: str,
    letter_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        letter_id,
        endpoint=_EP_LETTERS,
        label="letter",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_letter(
    api_key: str,
    letter_id: str,
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_cancel(
        api_key,
        letter_id,
        endpoint=_EP_LETTERS,
        label="letter",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Self-mailers
# ---------------------------------------------------------------------------


def create_self_mailer(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_SELF_MAILERS,
        label="self mailer",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_self_mailers(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_SELF_MAILERS,
        label="self mailers",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_self_mailer(
    api_key: str,
    self_mailer_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        self_mailer_id,
        endpoint=_EP_SELF_MAILERS,
        label="self mailer",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_self_mailer(
    api_key: str,
    self_mailer_id: str,
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_cancel(
        api_key,
        self_mailer_id,
        endpoint=_EP_SELF_MAILERS,
        label="self mailer",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Snap packs
# ---------------------------------------------------------------------------


def create_snap_pack(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_SNAP_PACKS,
        label="snap pack",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_snap_packs(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_SNAP_PACKS,
        label="snap packs",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_snap_pack(
    api_key: str,
    snap_pack_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        snap_pack_id,
        endpoint=_EP_SNAP_PACKS,
        label="snap pack",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_snap_pack(
    api_key: str,
    snap_pack_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_cancel(
        api_key,
        snap_pack_id,
        endpoint=_EP_SNAP_PACKS,
        label="snap pack",
        idempotency_key=None,
        idempotency_in_query=False,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Booklets
# ---------------------------------------------------------------------------


def create_booklet(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_BOOKLETS,
        label="booklet",
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_booklets(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_BOOKLETS,
        label="booklets",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_booklet(
    api_key: str,
    booklet_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        booklet_id,
        endpoint=_EP_BOOKLETS,
        label="booklet",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_booklet(
    api_key: str,
    booklet_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_cancel(
        api_key,
        booklet_id,
        endpoint=_EP_BOOKLETS,
        label="booklet",
        idempotency_key=None,
        idempotency_in_query=False,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Templates + template versions (Lob-hosted; thin proxy)
# ---------------------------------------------------------------------------


def create_template(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_TEMPLATES,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create template response type")
    return data


def list_templates(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_TEMPLATES,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list templates response type")
    return data


def get_template(
    api_key: str,
    template_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_TEMPLATES}/{template_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get template response type")
    return data


def update_template(
    api_key: str,
    template_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_TEMPLATES}/{template_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update template response type")
    return data


def delete_template(
    api_key: str,
    template_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_TEMPLATES}/{template_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete template response type")
    return data


def create_template_version(
    api_key: str,
    template_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_TEMPLATES}/{template_id}/versions",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create template version response type")
    return data


def list_template_versions(
    api_key: str,
    template_id: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_TEMPLATES}/{template_id}/versions",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list template versions response type")
    return data


def get_template_version(
    api_key: str,
    template_id: str,
    version_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_TEMPLATES}/{template_id}/versions/{version_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get template version response type")
    return data


def update_template_version(
    api_key: str,
    template_id: str,
    version_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_TEMPLATES}/{template_id}/versions/{version_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update template version response type")
    return data


def delete_template_version(
    api_key: str,
    template_id: str,
    version_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_TEMPLATES}/{template_id}/versions/{version_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete template version response type")
    return data


# ---------------------------------------------------------------------------
# Saved addresses
# ---------------------------------------------------------------------------


def create_address(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_ADDRESSES,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create address response type")
    return data


def list_addresses(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_ADDRESSES,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list addresses response type")
    return data


def get_address(
    api_key: str,
    address_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_ADDRESSES}/{address_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get address response type")
    return data


def delete_address(
    api_key: str,
    address_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_ADDRESSES}/{address_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete address response type")
    return data


# ---------------------------------------------------------------------------
# Buckslips + buckslip orders
# ---------------------------------------------------------------------------


def create_buckslip(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_BUCKSLIPS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create buckslip response type")
    return data


def list_buckslips(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_BUCKSLIPS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list buckslips response type")
    return data


def get_buckslip(
    api_key: str,
    buckslip_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_BUCKSLIPS}/{buckslip_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get buckslip response type")
    return data


def update_buckslip(
    api_key: str,
    buckslip_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_BUCKSLIPS}/{buckslip_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update buckslip response type")
    return data


def delete_buckslip(
    api_key: str,
    buckslip_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_BUCKSLIPS}/{buckslip_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete buckslip response type")
    return data


def create_buckslip_order(
    api_key: str,
    buckslip_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_BUCKSLIPS}/{buckslip_id}/orders",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create buckslip order response type")
    return data


def get_buckslip_order(
    api_key: str,
    buckslip_id: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_BUCKSLIPS}/{buckslip_id}/orders",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get buckslip order response type")
    return data


# ---------------------------------------------------------------------------
# Cards + card orders
# ---------------------------------------------------------------------------


def create_card(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_CARDS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create card response type")
    return data


def list_cards(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_CARDS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list cards response type")
    return data


def get_card(
    api_key: str,
    card_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_CARDS}/{card_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get card response type")
    return data


def update_card(
    api_key: str,
    card_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_CARDS}/{card_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update card response type")
    return data


def delete_card(
    api_key: str,
    card_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_CARDS}/{card_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete card response type")
    return data


def create_card_order(
    api_key: str,
    card_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_CARDS}/{card_id}/orders",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create card order response type")
    return data


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def create_campaign(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_CAMPAIGNS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create campaign response type")
    return data


def list_campaigns(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_CAMPAIGNS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list campaigns response type")
    return data


def get_campaign(
    api_key: str,
    campaign_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_CAMPAIGNS}/{campaign_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get campaign response type")
    return data


def update_campaign(
    api_key: str,
    campaign_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_CAMPAIGNS}/{campaign_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update campaign response type")
    return data


def delete_campaign(
    api_key: str,
    campaign_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_CAMPAIGNS}/{campaign_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob delete campaign response type")
    return data


def send_campaign(
    api_key: str,
    campaign_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_CAMPAIGNS}/{campaign_id}/send",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob send campaign response type")
    return data


# ---------------------------------------------------------------------------
# Creatives (Lob-hosted; thin proxy)
# ---------------------------------------------------------------------------


def create_creative(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_CREATIVES,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create creative response type")
    return data


def get_creative(
    api_key: str,
    creative_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_CREATIVES}/{creative_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get creative response type")
    return data


def update_creative(
    api_key: str,
    creative_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    idempotency_in_query: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_CREATIVES}/{creative_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
        idempotency_in_query=idempotency_in_query,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update creative response type")
    return data


# ---------------------------------------------------------------------------
# Uploads (bulk file ingestion + exports + report)
# ---------------------------------------------------------------------------


def create_upload(
    api_key: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_UPLOADS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create upload response type")
    return data


def list_uploads(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_UPLOADS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob list uploads response type")
    return data


def get_upload(
    api_key: str,
    upload_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_UPLOADS}/{upload_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get upload response type")
    return data


def update_upload(
    api_key: str,
    upload_id: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="PATCH",
        path=f"{_EP_UPLOADS}/{upload_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob update upload response type")
    return data


def delete_upload(
    api_key: str,
    upload_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    if not api_key:
        raise LobProviderError("Missing Lob API key")
    url = f"{_build_base_url(base_url)}{_EP_UPLOADS}/{upload_id}"
    try:
        response = _request_with_retry(
            method="DELETE",
            url=url,
            auth=_build_basic_auth(api_key),
            headers={"Accept": "application/json"},
            timeout_seconds=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise LobProviderError(f"Lob connectivity error: {exc}") from exc
    if response.status_code in {401, 403}:
        raise LobProviderError("Invalid Lob API key")
    if response.status_code == 204:
        return {"id": upload_id, "deleted": True}
    if response.status_code >= 400:
        raise LobProviderError(
            f"Lob API returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError:
        return {"id": upload_id, "deleted": True}


def upload_file(
    api_key: str,
    upload_id: str,
    *,
    file_name: str,
    file_content: bytes,
    content_type: str = "text/csv",
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    data = _request_multipart(
        path=f"{_EP_UPLOADS}/{upload_id}/file",
        api_key=api_key,
        file_name=file_name,
        file_content=file_content,
        content_type=content_type,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob upload file response type")
    return data


def create_upload_export(
    api_key: str,
    upload_id: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_UPLOADS}/{upload_id}/exports",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob create upload export response type")
    return data


def get_upload_export(
    api_key: str,
    upload_id: str,
    export_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_UPLOADS}/{upload_id}/exports/{export_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get upload export response type")
    return data


def get_upload_report(
    api_key: str,
    upload_id: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_UPLOADS}/{upload_id}/report",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise LobProviderError("Unexpected Lob get upload report response type")
    return data
