"""Async HTTP client for the Blitz enrichment API.

Translation layer for the `/internal/tasks/enrich` proxy. Each Trigger.dev
fan-out call lands a single Blitz POST: the proxy passes the action slug
plus the entity payload, and this module materializes the
provider-specific request body and dispatches it.

Auth: ``x-api-key: <BLITZAPI_API_KEY>``. Base: ``https://api.blitz-api.ai``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class BlitzError(Exception):
    pass


class BlitzNotConfiguredError(BlitzError):
    pass


class BlitzUnsupportedActionError(BlitzError):
    pass


class BlitzCallError(BlitzError):
    def __init__(self, *, status_code: int, body: str, endpoint: str) -> None:
        super().__init__(
            f"Blitz {endpoint} returned HTTP {status_code}: {body[:500]}"
        )
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint


def _api_key_or_raise() -> str:
    secret = settings.BLITZAPI_API_KEY
    if not secret:
        raise BlitzNotConfiguredError(
            "BLITZAPI_API_KEY is not configured — cannot call Blitz"
        )
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    return str(secret)


def _base_url() -> str:
    return (settings.BLITZAPI_API_BASE or "https://api.blitz-api.ai").rstrip("/")


def build_request(
    action: str, entity_data: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Translate (action, entity_data) into (endpoint_path, request_body).

    Raises ``BlitzUnsupportedActionError`` when the action slug isn't in
    the matrix or the entity payload is missing the routing field the
    upstream endpoint requires.
    """
    if action == "find_work_email":
        linkedin_url = entity_data.get("linkedin_url")
        if not linkedin_url:
            raise BlitzUnsupportedActionError(
                "find_work_email requires entity_data.linkedin_url"
            )
        return "/v2/enrichment/email", {"person_linkedin_url": linkedin_url}

    raise BlitzUnsupportedActionError(
        f"unsupported blitz action: {action!r}"
    )


async def call(
    action: str,
    entity_data: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST to Blitz for the given action. Returns the parsed JSON body.

    Raises:
      * ``BlitzNotConfiguredError`` — no API key.
      * ``BlitzUnsupportedActionError`` — bad action / missing routing field.
      * ``BlitzCallError`` — non-2xx HTTP from upstream.
      * ``httpx.HTTPError`` subclasses — network / timeout failures bubble.
    """
    path, body = build_request(action, entity_data)
    headers = {
        "x-api-key": _api_key_or_raise(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code // 100 != 2:
        raise BlitzCallError(
            status_code=resp.status_code,
            body=resp.text,
            endpoint=path,
        )
    return resp.json()
