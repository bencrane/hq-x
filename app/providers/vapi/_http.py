from __future__ import annotations

import random
import time
from typing import Any

import httpx


VAPI_BASE_URL = "https://api.vapi.ai"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0


class VapiProviderError(Exception):
    """Provider-level exception for Vapi integration failures."""

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
            "invalid vapi credentials" in message
            or "vapi auth" in message
            or "endpoint not found" in message
            or "unexpected vapi" in message
            or "bad request" in message
        ):
            return "terminal"
        return "unknown"

    @property
    def retryable(self) -> bool:
        return self.category == "transient"


def _request_with_retry(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> httpx.Response:
    """HTTP request with retry on 429/5xx."""
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

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRY_ATTEMPTS:
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


def request(
    method: str,
    path: str,
    api_key: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> Any:
    """Make an authenticated Vapi API request and return parsed JSON response."""
    if not api_key:
        raise VapiProviderError("Invalid Vapi credentials: missing api_key")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{VAPI_BASE_URL}{path}"

    try:
        response = _request_with_retry(
            method=method,
            url=url,
            headers=headers,
            timeout_seconds=timeout,
            params=params,
            json_payload=json,
        )
    except httpx.HTTPError as exc:
        raise VapiProviderError(f"Vapi connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise VapiProviderError("Invalid Vapi credentials: Vapi auth failed")
    if response.status_code == 404:
        raise VapiProviderError(f"Vapi endpoint not found: {path}")
    if response.status_code == 400:
        raise VapiProviderError(f"Vapi bad request: {response.text[:300]}")
    if response.status_code >= 400:
        raise VapiProviderError(
            f"Vapi API returned HTTP {response.status_code}: {response.text[:300]}"
        )

    if response.status_code == 204:
        return None

    try:
        return response.json()
    except ValueError as exc:
        raise VapiProviderError("Unexpected Vapi response: non-JSON body") from exc
