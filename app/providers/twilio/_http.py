from __future__ import annotations

import random
import time
from typing import Any

import httpx


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0


class TwilioProviderError(Exception):
    """Provider-level exception for Twilio integration failures."""

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
            "invalid twilio credentials" in message
            or "twilio auth" in message
            or "endpoint not found" in message
            or "unexpected twilio" in message
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
    auth: httpx.BasicAuth,
    timeout_seconds: float,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    data: dict[str, Any] | list[tuple[str, str]] | None = None,
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
                    params=params,
                    json=json_payload,
                    data=data,
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


def request_json(
    *,
    method: str,
    url: str,
    account_sid: str,
    auth_token: str,
    timeout_seconds: float = 10.0,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    data: dict[str, Any] | list[tuple[str, str]] | None = None,
) -> Any:
    if not account_sid or not auth_token:
        raise TwilioProviderError("Invalid Twilio credentials: missing account_sid or auth_token")

    auth = httpx.BasicAuth(account_sid, auth_token)
    try:
        response = _request_with_retry(
            method=method,
            url=url,
            auth=auth,
            timeout_seconds=timeout_seconds,
            params=params,
            json_payload=json_payload,
            data=data,
        )
    except httpx.HTTPError as exc:
        raise TwilioProviderError(f"Twilio connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise TwilioProviderError("Invalid Twilio credentials: Twilio auth failed")
    if response.status_code == 404:
        raise TwilioProviderError(f"Twilio endpoint not found: {url}")
    if response.status_code == 400:
        raise TwilioProviderError(f"Twilio bad request: {response.text[:300]}")
    if response.status_code >= 400:
        raise TwilioProviderError(
            f"Twilio API returned HTTP {response.status_code}: {response.text[:300]}"
        )

    if response.status_code == 204:
        return None

    try:
        return response.json()
    except ValueError as exc:
        raise TwilioProviderError("Unexpected Twilio response: non-JSON body") from exc


def request_no_content(
    *,
    method: str,
    url: str,
    account_sid: str,
    auth_token: str,
    timeout_seconds: float = 10.0,
    data: dict[str, Any] | None = None,
) -> None:
    if not account_sid or not auth_token:
        raise TwilioProviderError("Invalid Twilio credentials: missing account_sid or auth_token")

    auth = httpx.BasicAuth(account_sid, auth_token)
    try:
        response = _request_with_retry(
            method=method,
            url=url,
            auth=auth,
            timeout_seconds=timeout_seconds,
            data=data,
        )
    except httpx.HTTPError as exc:
        raise TwilioProviderError(f"Twilio connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise TwilioProviderError("Invalid Twilio credentials: Twilio auth failed")
    if response.status_code == 404:
        raise TwilioProviderError(f"Twilio endpoint not found: {url}")
    if response.status_code >= 400:
        raise TwilioProviderError(
            f"Twilio API returned HTTP {response.status_code}: {response.text[:300]}"
        )
