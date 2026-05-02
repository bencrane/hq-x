"""Thin async client for the data-engine-x (DEX) HTTP surface.

DEX accepts two auth modes on its require_flexible_auth dependency, both
sent as `Authorization: Bearer <token>`:

  1. Super-admin API key (string compare against DEX's SUPER_ADMIN_API_KEY).
     Used for server-to-server hq-x → DEX calls without a user JWT (seed
     scripts, reconciliation jobs, etc.).
  2. hq-x Supabase ES256 JWT (validated by DEX via JWKS). Used when a
     user-initiated request flows through hq-x and we want DEX to see the
     same identity.

This client tries the caller-supplied bearer first; if absent, falls back
to settings.DEX_SUPER_ADMIN_API_KEY. If neither is available, raises
DexAuthMissingError so the route can return a structured 502/503 instead
of a vague httpx error.

Responses are unwrapped from DEX's `{"data": ...}` envelope at the client
boundary so callers get the inner dict directly. Non-2xx responses raise
DexCallError(status_code, body) with the body preserved for logging.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0


class DexClientError(Exception):
    """Base for DEX client errors."""


class DexNotConfiguredError(DexClientError):
    """settings.DEX_BASE_URL is unset; the client cannot make any call."""


class DexAuthMissingError(DexClientError):
    """No bearer token provided and no super-admin API key configured."""


class DexCallError(DexClientError):
    """DEX returned a non-2xx response."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"dex call failed: status={status_code} body={body!r}")
        self.status_code = status_code
        self.body = body


def _base_url() -> str:
    base = settings.DEX_BASE_URL
    if not base:
        raise DexNotConfiguredError("DEX_BASE_URL is not set")
    return base.rstrip("/")


def _auth_header(bearer_token: str | None) -> dict[str, str]:
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    api_key = settings.DEX_SUPER_ADMIN_API_KEY
    if api_key is not None:
        return {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    raise DexAuthMissingError(
        "no bearer token provided and DEX_SUPER_ADMIN_API_KEY is not set"
    )


def _unwrap(payload: Any) -> Any:
    """DEX wraps successful responses in {"data": ...}. Return the inner dict."""
    if isinstance(payload, dict) and "data" in payload and len(payload) == 1:
        return payload["data"]
    return payload


async def _request(
    method: str,
    path: str,
    *,
    bearer_token: str | None,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> Any:
    url = f"{_base_url()}{path}"
    headers = _auth_header(bearer_token)
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        try:
            resp = await client.request(
                method, url, headers=headers, json=json, params=params,
            )
        except httpx.HTTPError as exc:
            logger.warning("dex_request_failed method=%s path=%s err=%r", method, path, exc)
            raise DexCallError(599, str(exc)) from exc

    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON error body is fine
            body = resp.text
        raise DexCallError(resp.status_code, body)

    try:
        return _unwrap(resp.json())
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------
# Public methods
# ---------------------------------------------------------------------------


async def create_audience_spec(
    *,
    template_id: UUID,
    filter_overrides: dict[str, Any] | None = None,
    name: str | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/fmcsa/audience-specs."""
    body: dict[str, Any] = {
        "template_id": str(template_id),
        "filter_overrides": filter_overrides or {},
    }
    if name is not None:
        body["name"] = name
    return await _request(
        "POST", "/api/v1/fmcsa/audience-specs",
        bearer_token=bearer_token, json=body,
    )


async def get_audience_spec(
    spec_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/fmcsa/audience-specs/{spec_id}."""
    return await _request(
        "GET", f"/api/v1/fmcsa/audience-specs/{spec_id}",
        bearer_token=bearer_token,
    )


async def get_audience_template_by_slug(
    slug: str, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/fmcsa/audience-templates/{slug}."""
    return await _request(
        "GET", f"/api/v1/fmcsa/audience-templates/{slug}",
        bearer_token=bearer_token,
    )


async def list_audience_templates(
    *,
    partner_type: str | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/fmcsa/audience-templates."""
    params = {"partner_type": partner_type} if partner_type else None
    return await _request(
        "GET", "/api/v1/fmcsa/audience-templates",
        bearer_token=bearer_token, params=params,
    )


async def get_audience_descriptor(
    spec_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/fmcsa/audience-specs/{spec_id}/descriptor.

    Returns spec + template + derived audience_attributes — see DEX
    audience_templates_v1.get_audience_descriptor.
    """
    return await _request(
        "GET", f"/api/v1/fmcsa/audience-specs/{spec_id}/descriptor",
        bearer_token=bearer_token,
    )


async def count_audience_members(
    spec_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/fmcsa/audience-specs/{spec_id}/count.

    Returns {total, mv_sources, generated_at}.
    """
    return await _request(
        "POST", f"/api/v1/fmcsa/audience-specs/{spec_id}/count",
        bearer_token=bearer_token, json={},
    )


async def list_audience_members(
    spec_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/fmcsa/audience-specs/{spec_id}/preview.

    Returns {items, total, has_more, limit, offset, mv_sources, generated_at}.
    Items are FMCSA carrier rows — these are the per-member rows for DM
    creative work.
    """
    return await _request(
        "POST", f"/api/v1/fmcsa/audience-specs/{spec_id}/preview",
        bearer_token=bearer_token,
        json={"limit": limit, "offset": offset},
    )
