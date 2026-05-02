"""PostGrid Print & Mail HTTP client.

Hand-rolled httpx wrapper around the PostGrid Print & Mail v1 API.
Auth: x-api-key header (not HTTP Basic — see §4 of research notes).
Idempotency: Idempotency-Key request header on POST endpoints.

Test-mode guard: when APP_ENV != production, this client refuses API keys
that are NOT prefixed with 'test_'. This ensures no live mail is dispatched
from test/benchmark runs.

9 resource families: letters, postcards, cheques, self_mailers,
return_envelopes (flag on letters — list/get only), templates, contacts,
webhooks, tracking_events.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx

POSTGRID_PRINT_MAIL_API_BASE = "https://api.postgrid.com/print-mail/v1"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0

_EP_LETTERS = "/letters"
_EP_POSTCARDS = "/postcards"
_EP_CHEQUES = "/cheques"
_EP_SELF_MAILERS = "/selfmailers"
_EP_TEMPLATES = "/templates"
_EP_CONTACTS = "/contacts"
_EP_WEBHOOKS = "/webhooks"
_EP_TRACKING_EVENTS = "/letters"  # tracking events are per-letter sub-resource


class PostGridProviderError(Exception):
    """Provider-level exception for PostGrid integration failures."""

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
            "invalid postgrid api key" in message
            or "endpoint not found" in message
            or "missing postgrid api key" in message
            or "test key required" in message
            or "unexpected postgrid" in message
        ):
            return "terminal"
        return "unknown"

    @property
    def retryable(self) -> bool:
        return self.category == "transient"


def _guard_test_key(api_key: str) -> None:
    """Refuse live keys when not in production.

    PostGrid test keys are prefixed with 'test_'. Live keys start with
    'live_'. When APP_ENV is not 'production', live keys are rejected
    to prevent accidental live-mail dispatch from tests/benchmarks.
    """
    app_env = os.environ.get("APP_ENV", "").lower()
    if app_env == "production":
        return
    if not api_key.startswith("test_"):
        raise PostGridProviderError(
            "PostGrid test key required (must start with 'test_') "
            f"when APP_ENV != production (APP_ENV={app_env!r}). "
            "Refusing to use live key outside of production."
        )


def _build_base_url(base_url: str | None) -> str:
    return (base_url or POSTGRID_PRINT_MAIL_API_BASE).rstrip("/")


def _build_api_key_header(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


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
) -> Any:
    if not api_key:
        raise PostGridProviderError("Missing PostGrid API key")

    _guard_test_key(api_key)

    request_headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **_build_api_key_header(api_key),
    }

    if idempotency_key is not None:
        idempotency_key_stripped = idempotency_key.strip()
        if not idempotency_key_stripped:
            raise PostGridProviderError("Idempotency key must be non-empty when provided")
        request_headers["Idempotency-Key"] = idempotency_key_stripped

    url = f"{_build_base_url(base_url)}{path}"
    try:
        response = _request_with_retry(
            method=method,
            url=url,
            headers=request_headers,
            timeout_seconds=timeout_seconds,
            params=params,
            json_payload=json_payload,
        )
    except httpx.HTTPError as exc:
        raise PostGridProviderError(f"PostGrid connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise PostGridProviderError("Invalid PostGrid API key")
    if response.status_code == 404:
        raise PostGridProviderError(f"PostGrid endpoint not found: {path}")
    if response.status_code >= 400:
        raise PostGridProviderError(
            f"PostGrid API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise PostGridProviderError("PostGrid returned non-JSON response") from exc


def validate_api_key(
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 8.0,
) -> None:
    """Validate the API key by making a minimal authenticated request."""
    _request_json(
        method="GET",
        path=_EP_LETTERS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params={"limit": 1},
    )


# ---------------------------------------------------------------------------
# Generic CRUD helpers — same 4-call shape as Lob (create/list/get/cancel)
# ---------------------------------------------------------------------------


def _piece_create(
    api_key: str,
    payload: dict[str, Any],
    *,
    endpoint: str,
    label: str,
    idempotency_key: str | None,
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
    )
    if not isinstance(data, dict):
        raise PostGridProviderError(f"Unexpected PostGrid create {label} response type")
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
        raise PostGridProviderError(f"Unexpected PostGrid list {label} response type")
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
        raise PostGridProviderError(f"Unexpected PostGrid get {label} response type")
    return data


def _piece_delete(
    api_key: str,
    piece_id: str,
    *,
    endpoint: str,
    label: str,
    base_url: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{endpoint}/{piece_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError(f"Unexpected PostGrid delete/cancel {label} response type")
    return data


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------


def create_letter(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_LETTERS,
        label="letter",
        idempotency_key=idempotency_key,
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
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_delete(
        api_key,
        letter_id,
        endpoint=_EP_LETTERS,
        label="letter",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Postcards
# ---------------------------------------------------------------------------


def create_postcard(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_POSTCARDS,
        label="postcard",
        idempotency_key=idempotency_key,
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
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_delete(
        api_key,
        postcard_id,
        endpoint=_EP_POSTCARDS,
        label="postcard",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Cheques
# ---------------------------------------------------------------------------


def create_cheque(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_CHEQUES,
        label="cheque",
        idempotency_key=idempotency_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def list_cheques(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_list(
        api_key,
        endpoint=_EP_CHEQUES,
        label="cheques",
        params=params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_cheque(
    api_key: str,
    cheque_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_get(
        api_key,
        cheque_id,
        endpoint=_EP_CHEQUES,
        label="cheque",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def cancel_cheque(
    api_key: str,
    cheque_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_delete(
        api_key,
        cheque_id,
        endpoint=_EP_CHEQUES,
        label="cheque",
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
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_create(
        api_key,
        payload,
        endpoint=_EP_SELF_MAILERS,
        label="self mailer",
        idempotency_key=idempotency_key,
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
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    return _piece_delete(
        api_key,
        self_mailer_id,
        endpoint=_EP_SELF_MAILERS,
        label="self mailer",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Return envelopes — PostGrid treats this as a flag on letters, not a
# separate trackable artifact. These endpoints list/get letters that have
# the return_envelope flag set. There is no separate create endpoint.
# ---------------------------------------------------------------------------


def list_return_envelopes(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """List letters that have returnEnvelope=true."""
    merged_params = dict(params or {})
    merged_params["returnEnvelope"] = "true"
    return _piece_list(
        api_key,
        endpoint=_EP_LETTERS,
        label="return envelopes",
        params=merged_params,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_return_envelope(
    api_key: str,
    letter_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Get a letter that has returnEnvelope=true by its letter id."""
    return _piece_get(
        api_key,
        letter_id,
        endpoint=_EP_LETTERS,
        label="return envelope",
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def create_template(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
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
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid create template response type")
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
        raise PostGridProviderError("Unexpected PostGrid list templates response type")
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
        raise PostGridProviderError("Unexpected PostGrid get template response type")
    return data


def update_template(
    api_key: str,
    template_id: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=f"{_EP_TEMPLATES}/{template_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid update template response type")
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
        raise PostGridProviderError("Unexpected PostGrid delete template response type")
    return data


# ---------------------------------------------------------------------------
# Contacts (PostGrid's equivalent of Lob's saved addresses)
# ---------------------------------------------------------------------------


def create_contact(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_CONTACTS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid create contact response type")
    return data


def list_contacts(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_CONTACTS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid list contacts response type")
    return data


def get_contact(
    api_key: str,
    contact_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=f"{_EP_CONTACTS}/{contact_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid get contact response type")
    return data


def delete_contact(
    api_key: str,
    contact_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_CONTACTS}/{contact_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid delete contact response type")
    return data


# ---------------------------------------------------------------------------
# Webhooks (subscription management)
# ---------------------------------------------------------------------------


def create_webhook(
    api_key: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        path=_EP_WEBHOOKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        json_payload=payload,
        idempotency_key=idempotency_key,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid create webhook response type")
    return data


def list_webhooks(
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="GET",
        path=_EP_WEBHOOKS,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid list webhooks response type")
    return data


def get_webhook(
    api_key: str,
    webhook_id: str,
    *,
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
        raise PostGridProviderError("Unexpected PostGrid get webhook response type")
    return data


def delete_webhook(
    api_key: str,
    webhook_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    data = _request_json(
        method="DELETE",
        path=f"{_EP_WEBHOOKS}/{webhook_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid delete webhook response type")
    return data


# ---------------------------------------------------------------------------
# Tracking events (per-letter event history)
# ---------------------------------------------------------------------------


def list_tracking_events(
    api_key: str,
    letter_id: str,
    *,
    params: dict[str, Any] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """List tracking events for a specific letter."""
    data = _request_json(
        method="GET",
        path=f"{_EP_LETTERS}/{letter_id}/events",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        params=params,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid list tracking events response type")
    return data


def get_tracking_event(
    api_key: str,
    letter_id: str,
    event_id: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Get a single tracking event for a letter."""
    data = _request_json(
        method="GET",
        path=f"{_EP_LETTERS}/{letter_id}/events/{event_id}",
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(data, dict):
        raise PostGridProviderError("Unexpected PostGrid get tracking event response type")
    return data
