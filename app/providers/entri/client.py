"""Entri HTTP client.

Hand-rolled httpx wrapper around the documented Entri REST API. Single-tenant:
one partner account ($ENTRI_APPLICATION_ID + $ENTRI_SECRET) covers every
customer domain. Mirrors `app/providers/dub/client.py` for retry/error
semantics so the rest of the codebase looks consistent.

Endpoints we wrap (per https://developers.entri.com):
  POST /token          - mint a 60-min JWT for the showEntri SDK
  GET  /power          - eligibility check (CNAME present?)
  POST /power          - register a custom-domain → applicationUrl mapping
  PUT  /power          - update an existing mapping (idempotent re-register)
  DELETE /power        - unregister a custom-domain mapping

JWT lifetime is 60 minutes; we cache per-session in domain_connections rather
than globally because Entri scopes tokens to (applicationId, session).
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

DEFAULT_API_BASE = "https://api.goentri.com"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0

_EP_TOKEN = "/token"
_EP_POWER = "/power"


class EntriProviderError(Exception):
    """Raised on any non-2xx Entri response or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

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


def _base_url(override: str | None) -> str:
    return (override or DEFAULT_API_BASE).rstrip("/")


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
        return response

    if last_exc:
        raise last_exc
    assert response is not None
    return response


def _raise_from_response(response: httpx.Response) -> None:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None

    code: str | None = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or f"HTTP {status}"
        code = body.get("code") if isinstance(body.get("code"), str) else None
    else:
        message = f"HTTP {status}: {response.text[:200]}"

    raise EntriProviderError(str(message), status=status, code=code)


def mint_token(
    *,
    application_id: str,
    secret: str,
    base_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """POST /token — exchange (applicationId, secret) for a 60-min JWT.

    Response shape (per docs): {"auth_token": "<jwt>"}.
    """
    if not application_id or not secret:
        raise EntriProviderError("Missing Entri credentials", status=None)

    url = f"{_base_url(base_url)}{_EP_TOKEN}"
    response = _request_with_retry(
        method="POST",
        url=url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout_seconds=timeout_seconds,
        json_payload={"applicationId": application_id, "secret": secret},
    )
    if response.status_code >= 400:
        _raise_from_response(response)
    try:
        return response.json()
    except ValueError as exc:
        raise EntriProviderError("Token response not JSON", status=response.status_code) from exc


def _power_headers(*, application_id: str, jwt: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": jwt,
        "applicationId": application_id,
    }


def check_power_eligibility(
    *,
    application_id: str,
    jwt: str,
    domain: str,
    root_domain: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """GET /power — does the customer have the required CNAME / A record yet?

    Returns whatever Entri returns; the canonical field is `eligible: bool`.
    """
    url = f"{_base_url(base_url)}{_EP_POWER}"
    response = _request_with_retry(
        method="GET",
        url=url,
        headers=_power_headers(application_id=application_id, jwt=jwt),
        timeout_seconds=timeout_seconds,
        params={"domain": domain, "rootDomain": str(root_domain).lower()},
    )
    if response.status_code >= 400:
        _raise_from_response(response)
    try:
        return response.json()
    except ValueError:
        return {}


def register_power_domain(
    *,
    application_id: str,
    jwt: str,
    domain: str,
    application_url: str,
    power_root_path_access: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """POST /power — first-time registration of a custom-domain mapping."""
    payload: dict[str, Any] = {
        "domain": domain,
        "applicationUrl": application_url,
    }
    if power_root_path_access:
        payload["powerRootPathAccess"] = list(power_root_path_access)

    url = f"{_base_url(base_url)}{_EP_POWER}"
    response = _request_with_retry(
        method="POST",
        url=url,
        headers=_power_headers(application_id=application_id, jwt=jwt),
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if response.status_code >= 400:
        _raise_from_response(response)
    try:
        return response.json()
    except ValueError:
        return {}


def update_power_domain(
    *,
    application_id: str,
    jwt: str,
    domain: str,
    application_url: str,
    power_root_path_access: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """PUT /power — idempotent re-registration / update."""
    payload: dict[str, Any] = {
        "domain": domain,
        "applicationUrl": application_url,
    }
    if power_root_path_access:
        payload["powerRootPathAccess"] = list(power_root_path_access)

    url = f"{_base_url(base_url)}{_EP_POWER}"
    response = _request_with_retry(
        method="PUT",
        url=url,
        headers=_power_headers(application_id=application_id, jwt=jwt),
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if response.status_code >= 400:
        _raise_from_response(response)
    try:
        return response.json()
    except ValueError:
        return {}


def delete_power_domain(
    *,
    application_id: str,
    jwt: str,
    domain: str,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """DELETE /power — disconnect a custom domain."""
    url = f"{_base_url(base_url)}{_EP_POWER}"
    response = _request_with_retry(
        method="DELETE",
        url=url,
        headers=_power_headers(application_id=application_id, jwt=jwt),
        timeout_seconds=timeout_seconds,
        json_payload={"domain": domain},
    )
    if response.status_code >= 400:
        _raise_from_response(response)
    try:
        return response.json()
    except ValueError:
        return {}
