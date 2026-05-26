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

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0

# Retry-on-transient policy. DEX runs on Railway; rolling deploys can return
# 502/503/504 from the edge ("Application failed to respond") for ~5-30s.
# We retry idempotent methods only (GET/HEAD/OPTIONS) — POST/PUT/PATCH/DELETE
# could double-execute.
_RETRY_STATUSES = frozenset({502, 503, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.25, 0.75, 2.0)  # 3 retries = 4 total attempts


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
    method_upper = method.upper()
    retry_eligible = method_upper in _IDEMPOTENT_METHODS

    resp: httpx.Response | None = None
    last_transport_exc: httpx.HTTPError | None = None

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, json=json, params=params,
                )
                last_transport_exc = None
            except httpx.HTTPError as exc:
                last_transport_exc = exc
                resp = None
                if retry_eligible and attempt < len(_RETRY_BACKOFF_SECONDS):
                    delay = _RETRY_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "dex_request_transport_retry attempt=%d/%d method=%s path=%s err=%r delay=%.2fs",
                        attempt + 1, len(_RETRY_BACKOFF_SECONDS) + 1,
                        method, path, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

            if (
                retry_eligible
                and resp.status_code in _RETRY_STATUSES
                and attempt < len(_RETRY_BACKOFF_SECONDS)
            ):
                delay = _RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "dex_request_5xx_retry attempt=%d/%d method=%s path=%s status=%d delay=%.2fs",
                    attempt + 1, len(_RETRY_BACKOFF_SECONDS) + 1,
                    method, path, resp.status_code, delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    if resp is None:
        # All transport attempts exhausted (or first attempt failed on a non-retry method).
        assert last_transport_exc is not None
        logger.warning(
            "dex_request_failed method=%s path=%s err=%r",
            method, path, last_transport_exc,
        )
        raise DexCallError(599, str(last_transport_exc)) from last_transport_exc

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


async def list_coverage_stats(
    *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /coverage/stats — coverage meta-stats (datasets, bridges, intersections).

    Pure passthrough; DEX returns `{datasets, bridges, intersections}`
    directly (no envelope). Auth via DEX_SERVICE_TOKEN when no caller
    bearer is supplied.
    """
    return await _request(
        "GET", "/coverage/stats",
        bearer_token=bearer_token,
    )


# ---------------------------------------------------------------------------
# GTM Views (gtm.views in DEX — operator-authored materialized-view defs).
# Renamed from gtm.audiences on 2026-05-25; same primitive, clearer vocabulary
# (audiences are now reserved for the disposable campaign-cohort layer that
# lands in a follow-up cycle).
# ---------------------------------------------------------------------------


async def list_gtm_views(*, bearer_token: str | None = None) -> dict[str, Any]:
    """GET /api/v1/gtm/views. Returns {"views": [...]}."""
    return await _request(
        "GET", "/api/v1/gtm/views",
        bearer_token=bearer_token,
    )


async def get_gtm_view(
    view_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/gtm/views/{id}. Returns {"view": {...}}."""
    return await _request(
        "GET", f"/api/v1/gtm/views/{view_id}",
        bearer_token=bearer_token,
    )


async def create_gtm_view(
    spec: dict[str, Any], *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/gtm/views with the spec body. Returns {"view": {...}}."""
    return await _request(
        "POST", "/api/v1/gtm/views",
        bearer_token=bearer_token, json=spec,
    )


async def patch_gtm_view(
    view_id: UUID,
    patch: dict[str, Any],
    *,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """PATCH /api/v1/gtm/views/{id}. Returns {"view": {...}}."""
    return await _request(
        "PATCH", f"/api/v1/gtm/views/{view_id}",
        bearer_token=bearer_token, json=patch,
    )


async def delete_gtm_view(
    view_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """DELETE /api/v1/gtm/views/{id}. Returns {"deleted": "<uuid>"}."""
    return await _request(
        "DELETE", f"/api/v1/gtm/views/{view_id}",
        bearer_token=bearer_token,
    )


async def compute_gtm_view(
    view_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/gtm/views/{id}/compute. Returns {"view": {...}} with
    fresh computed_count + computed_at populated."""
    return await _request(
        "POST", f"/api/v1/gtm/views/{view_id}/compute",
        bearer_token=bearer_token, json={},
    )


async def materialize_gtm_view(
    view_id: UUID, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/gtm/views/{id}/materialize. Emits Lance dataset under
    polaris-warehouse/views/<slug>_lance/, registers in Polaris, returns
    {"view": {...}} with materialized_uri + materialized_at + row_count populated."""
    return await _request(
        "POST", f"/api/v1/gtm/views/{view_id}/materialize",
        bearer_token=bearer_token, json={},
    )


async def list_gtm_view_sources(
    *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/gtm/views/catalog/sources. Returns the Polaris-driven
    source catalog the agent / UI uses to author views."""
    return await _request(
        "GET", "/api/v1/gtm/views/catalog/sources",
        bearer_token=bearer_token,
    )


async def list_gtm_signals(*, bearer_token: str | None = None) -> dict[str, Any]:
    """GET /api/v1/gtm/signals. Read-only registry of configuration-driven
    GTM trigger rules (ops.gtm_signals). Returns {"signals": [...]} after
    _unwrap strips the DataEnvelope. Ordered is_active DESC, signal_slug ASC."""
    return await _request(
        "GET", "/api/v1/gtm/signals",
        bearer_token=bearer_token,
    )


async def patch_gtm_signal(
    signal_slug: str,
    patch: dict[str, Any],
    *,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """PATCH /api/v1/gtm/signals/{slug}. Partial update — body may include
    any subset of {webhook_test_url, webhook_prod_url, webhook_target,
    is_active}. Returns {"signal": {...}} after _unwrap."""
    return await _request(
        "PATCH", f"/api/v1/gtm/signals/{signal_slug}",
        bearer_token=bearer_token, json=patch,
    )


async def delete_gtm_signal(
    signal_slug: str, *, bearer_token: str | None = None,
) -> dict[str, Any]:
    """DELETE /api/v1/gtm/signals/{slug}. Hard-delete. Returns {"deleted": "<slug>"}."""
    return await _request(
        "DELETE", f"/api/v1/gtm/signals/{signal_slug}",
        bearer_token=bearer_token,
    )


async def fire_gtm_signal(
    signal_slug: str,
    body: dict[str, Any],
    *,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/gtm/signals/{slug}/fire. Spawns the Modal manual-fire
    function and returns {call_id, status: 'pending', slug} immediately.
    Caller polls fire_gtm_signal_status() with the call_id for the result.
    Body may include `target` ('test'|'prod') and/or `limit` (int)."""
    return await _request(
        "POST", f"/api/v1/gtm/signals/{signal_slug}/fire",
        bearer_token=bearer_token, json=body,
    )


async def get_gtm_signal(
    signal_slug: str,
    *,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Fetch a single signal definition by slug.

    DEX does not expose a per-slug GET today — we filter the list
    response client-side. Raises ``DexCallError(status_code=404)`` if
    the slug is not present in the list. Used by hq-x's
    ``/api/v1/signals/{slug}/run-agent`` to grab the signal name +
    criteria + spine_target before minting the agent run.
    """
    listing = await list_gtm_signals(bearer_token=bearer_token)
    for sig in listing.get("signals", []) or []:
        if sig.get("signal_slug") == signal_slug:
            return sig
    raise DexCallError(
        status_code=404,
        body=f"signal {signal_slug!r} not found in DEX",
    )


async def preview_signal_cohort(
    signal_slug: str,
    *,
    limit: int | None = None,
    target: str | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/gtm/signals/{slug}/preview. Non-dispatching cohort
    compile — same Lance+DuckDB compute the cron does, but returns the
    rows instead of POSTing them to a webhook. Returns
    ``{signal_slug, name, criteria, spine_target, target, webhook_url,
    matched_count, rows, limited}`` after _unwrap.
    """
    body: dict[str, Any] = {}
    if limit is not None:
        body["limit"] = limit
    if target is not None:
        body["target"] = target
    return await _request(
        "POST", f"/api/v1/gtm/signals/{signal_slug}/preview",
        bearer_token=bearer_token, json=body,
    )


async def fire_gtm_signal_status(
    call_id: str,
    *,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/gtm/signals/fire/status/{call_id}. Non-blocking poll of a
    previously-spawned fire. Returns either
        {"status": "pending", "call_id": "..."}
    or
        {"status": "done", "call_id": "...", "result": {...}}
    DEX raises 422 on per-signal errors (e.g. empty webhook URL) and 410 on
    expired call_ids (Modal retains results ~24h)."""
    return await _request(
        "GET", f"/api/v1/gtm/signals/fire/status/{call_id}",
        bearer_token=bearer_token,
    )


async def list_gtm_leads(
    *,
    source: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/internal/gtm/leads — paginated people-grain leads.

    DEX joins gtm.people to gtm.companies and returns `rows`, `total_count`,
    `limit`, `offset`. Each row carries its own `source` value, so callers
    can derive the universe of sources from the data without a separate
    distinct-sources endpoint.
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if source:
        params["source"] = source
    if q:
        params["q"] = q
    return await _request(
        "GET", "/api/internal/gtm/leads",
        bearer_token=bearer_token, params=params,
    )


async def get_companies_hydration_slice(
    *,
    bearer_token: str | None = None,
) -> list[dict[str, Any]]:
    """GET /api/v1/gtm/companies/hydration-slice — Phase 1 firmographic cohort.

    DEX reads the physical SAM ↔ PDL ↔ USAspending bridge Lance dataset and
    returns up to 11 `{uei, domain}` rows for Construction-NAICS recipients
    with lifetime obligations > $150K. The DataEnvelope wrapper is stripped
    by `_unwrap`, so this returns the raw array.
    """
    payload = await _request(
        "GET", "/api/v1/gtm/companies/hydration-slice",
        bearer_token=bearer_token,
    )
    if not isinstance(payload, list):
        raise DexCallError(200, payload)
    return payload


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
