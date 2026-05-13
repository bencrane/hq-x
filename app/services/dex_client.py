"""Thin async client for the data-engine-x (DEX) HTTP surface.

DEX accepts two auth modes on its require_flexible_auth dependency, both
sent as `Authorization: Bearer <token>`:

  1. Service token (string compare against DEX's `service_token` setting,
     env `DEX_SERVICE_TOKEN`). Used for server-to-server hq-x → DEX calls
     without a user JWT (seed scripts, reconciliation jobs, etc.).
  2. hq-x Supabase ES256 JWT (validated by DEX via JWKS). Used when a
     user-initiated request flows through hq-x and we want DEX to see the
     same identity.

This client tries the caller-supplied bearer first; if absent, falls back
to settings.DEX_SERVICE_TOKEN. If neither is available, raises
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
    """No bearer token provided and no DEX service token configured."""


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
    api_key = settings.DEX_SERVICE_TOKEN
    if api_key is not None:
        return {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    raise DexAuthMissingError(
        "no bearer token provided and DEX_SERVICE_TOKEN is not set"
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

    # Phase 0b: merge DEX-side lineage entries into hq-x's per-request tracker.
    # Runs on BOTH success and error paths — operator wants to know what data
    # the failed call would have read. record_catalog_read is a no-op when
    # called outside an HTTP request context (script / Trigger / Modal).
    _merge_dex_lineage(resp.headers.get("x-data-lineage"))

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


def _merge_dex_lineage(header_value: str | None) -> None:
    """Parse the X-Data-Lineage header from a DEX response and replay each
    entry into hq-x's per-request tracker. Tolerant of missing / malformed
    headers — never raises; never makes the upstream call appear to fail.
    """
    if not header_value:
        return
    try:
        import json as _json
        from datetime import datetime
        from app.services.lineage import record_catalog_read

        entries = _json.loads(header_value)
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            table = entry.get("table")
            fmt = entry.get("format")
            snapshot_id = entry.get("snapshot_id")
            if not table or not fmt:
                continue
            # Preserve upstream queried_at if present + parseable.
            queried_at_raw = entry.get("queried_at")
            queried_at = None
            if isinstance(queried_at_raw, str):
                try:
                    queried_at = datetime.fromisoformat(queried_at_raw)
                except ValueError:
                    queried_at = None
            record_catalog_read(
                table=table,
                snapshot_id=snapshot_id,
                format=fmt,
                queried_at=queried_at,
            )
    except Exception:  # noqa: BLE001 — lineage merge MUST NOT break callers
        logger.debug("lineage merge failed (non-fatal)", exc_info=True)


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


async def preview_self_prospect_audience(
    *,
    industries: list[str] | None = None,
    entity_role: str | None = "demand",
    sources: list[str] | None = None,
    title_patterns: list[str] | None = None,
    sample_size: int | None = 3,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/self-prospect-audience/preview.

    Filters the curated entities.new_target_companies / new_target_people
    subset (with title via JOIN to clay_find_people.latest_experience_title)
    and returns count + sample. Powers the demand-side audience-builder
    UI in hq-command. See DEX
    app/services/self_prospect_audience.py for filter semantics.

    Returns {filters, count_companies, count_people, sample_companies,
    sample_people}.
    """
    body: dict[str, Any] = {}
    if industries is not None:
        body["industries"] = industries
    if entity_role is not None:
        body["entity_role"] = entity_role
    if sources is not None:
        body["sources"] = sources
    if title_patterns is not None:
        body["title_patterns"] = title_patterns
    if sample_size is not None:
        body["sample_size"] = sample_size
    return await _request(
        "POST", "/api/v1/self-prospect-audience/preview",
        bearer_token=bearer_token, json=body,
    )


async def get_audience_member_gestalt(
    *,
    entity_type: str,
    entity_id: str,
    entity_sub_key: str = "",
    bearer_token: str | None = None,
) -> dict[str, Any] | None:
    """GET /api/v1/audience-member-gestalts/{entity_type}/{entity_id}.

    Returns the gestalt row for a member, or None when DEX has none.
    Tolerates 404 (the gestalt may not yet have been generated for
    this member); other errors propagate as DexCallError.
    """
    try:
        return await _request(
            "GET",
            f"/api/v1/audience-member-gestalts/{entity_type}/{entity_id}",
            bearer_token=bearer_token,
            params={"sub_key": entity_sub_key} if entity_sub_key else None,
        )
    except DexCallError as exc:
        if exc.status_code == 404:
            return None
        raise


async def upsert_audience_member_gestalt(
    *,
    entity_type: str,
    entity_id: str,
    entity_sub_key: str = "",
    gestalt_md: str,
    source_records: dict[str, Any] | None = None,
    generated_by: str = "audience-member-gestalt-agent",
    model: str | None = None,
    duration_ms: int | None = None,
    cost_dollars: float | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/audience-member-gestalts. Idempotent upsert."""
    payload: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_sub_key": entity_sub_key,
        "gestalt_md": gestalt_md,
        "source_records": source_records or {},
        "generated_by": generated_by,
    }
    if model is not None:
        payload["model"] = model
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if cost_dollars is not None:
        payload["cost_dollars"] = cost_dollars
    return await _request(
        "POST", "/api/v1/audience-member-gestalts",
        bearer_token=bearer_token, json=payload,
    )
