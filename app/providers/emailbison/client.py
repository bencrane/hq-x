"""EmailBison HTTP client.

Hand-rolled httpx wrapper around the EmailBison API. Single-tenant: every
function takes an explicit ``api_key`` (the ``ID|TOKEN`` workspace token);
no per-org credential plumbing. Subset port from the day-one shortlist in
``docs/emailbison-api-mcp-coverage.md`` §4 — warmup / blocklist / tags
CRUD / sender-email CRUD / schedule CRUD / sequence-step CRUD are
intentionally omitted.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

from app.config import settings

EMAILBISON_API_BASE = "https://app.outboundsolutions.com"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0


class EmailBisonProviderError(Exception):
    """Provider-level exception for EmailBison integration failures."""

    @property
    def category(self) -> str:
        message = str(self).lower()
        if (
            "connectivity error" in message
            or "emailbison http 429" in message
            or "emailbison http 500" in message
            or "emailbison http 502" in message
            or "emailbison http 503" in message
            or "emailbison http 504" in message
        ):
            return "transient"
        if (
            "invalid emailbison api key" in message
            or "missing emailbison api key" in message
            or "emailbison api_key not set" in message
            or "emailbison_api_key not set" in message
            or "endpoint not found" in message
        ):
            return "terminal"
        return "unknown"

    @property
    def retryable(self) -> bool:
        return self.category == "transient"


def _build_base_url(base_url: str | None) -> str:
    return (base_url or settings.EMAILBISON_API_BASE or EMAILBISON_API_BASE).rstrip("/")


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


def _request_json(
    *,
    method: str,
    path: str,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 15.0,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> Any:
    if not api_key:
        raise EmailBisonProviderError("Missing EmailBison API key")

    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

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
        raise EmailBisonProviderError(f"emailbison connectivity error: {exc}") from exc

    if response.status_code in {401, 403}:
        raise EmailBisonProviderError("invalid emailbison api key")
    if response.status_code == 404:
        raise EmailBisonProviderError(f"endpoint not found: {path}")
    if response.status_code >= 400:
        raise EmailBisonProviderError(
            f"emailbison http {response.status_code}: {response.text[:300]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise EmailBisonProviderError("emailbison returned non-JSON response") from exc


# ── Campaigns ─────────────────────────────────────────────────────────────


def create_campaign(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/campaigns. payload must include {"name": str, "type": str}."""
    return _request_json(
        method="POST", path="/api/campaigns", api_key=api_key, json_payload=payload
    )


def get_campaign(api_key: str, campaign_id: int | str) -> dict[str, Any]:
    """GET /api/campaigns/{id}."""
    return _request_json(
        method="GET", path=f"/api/campaigns/{campaign_id}", api_key=api_key
    )


def update_campaign(
    api_key: str, campaign_id: int | str, payload: dict[str, Any]
) -> dict[str, Any]:
    """PATCH /api/campaigns/{id}/update."""
    return _request_json(
        method="PATCH",
        path=f"/api/campaigns/{campaign_id}/update",
        api_key=api_key,
        json_payload=payload,
    )


def pause_campaign(api_key: str, campaign_id: int | str) -> dict[str, Any]:
    """PATCH /api/campaigns/{id}/pause."""
    return _request_json(
        method="PATCH",
        path=f"/api/campaigns/{campaign_id}/pause",
        api_key=api_key,
    )


def resume_campaign(api_key: str, campaign_id: int | str) -> dict[str, Any]:
    """PATCH /api/campaigns/{id}/resume."""
    return _request_json(
        method="PATCH",
        path=f"/api/campaigns/{campaign_id}/resume",
        api_key=api_key,
    )


def archive_campaign(api_key: str, campaign_id: int | str) -> dict[str, Any]:
    """PATCH /api/campaigns/{id}/archive."""
    return _request_json(
        method="PATCH",
        path=f"/api/campaigns/{campaign_id}/archive",
        api_key=api_key,
    )


def attach_leads(
    api_key: str,
    campaign_id: int | str,
    lead_ids: list[int],
    allow_parallel_sending: bool = False,
) -> dict[str, Any]:
    """POST /api/campaigns/{id}/leads/attach-leads."""
    return _request_json(
        method="POST",
        path=f"/api/campaigns/{campaign_id}/leads/attach-leads",
        api_key=api_key,
        json_payload={
            "lead_ids": lead_ids,
            "allow_parallel_sending": allow_parallel_sending,
        },
    )


def detach_leads(
    api_key: str, campaign_id: int | str, lead_ids: list[int]
) -> dict[str, Any]:
    """DELETE /api/campaigns/{id}/leads with json body {"lead_ids": [...]}."""
    return _request_json(
        method="DELETE",
        path=f"/api/campaigns/{campaign_id}/leads",
        api_key=api_key,
        json_payload={"lead_ids": lead_ids},
    )


def attach_sender_emails(
    api_key: str, campaign_id: int | str, sender_email_ids: list[int]
) -> dict[str, Any]:
    """POST /api/campaigns/{id}/attach-sender-emails."""
    return _request_json(
        method="POST",
        path=f"/api/campaigns/{campaign_id}/attach-sender-emails",
        api_key=api_key,
        json_payload={"sender_email_ids": sender_email_ids},
    )


# ── Leads ─────────────────────────────────────────────────────────────────


def upsert_lead(api_key: str, lead: dict[str, Any]) -> dict[str, Any]:
    """POST /api/leads/create-or-update/multiple with payload={"leads": [lead]}.

    Use the bulk endpoint for a single lead — it returns the canonical lead
    id and is idempotent by email.
    """
    response = _request_json(
        method="POST",
        path="/api/leads/create-or-update/multiple",
        api_key=api_key,
        json_payload={"leads": [lead]},
    )
    data = response.get("data") if isinstance(response, dict) else None
    if not data:
        raise EmailBisonProviderError(
            "emailbison upsert_lead returned empty data"
        )
    return response


def bulk_upsert_leads(
    api_key: str, leads: list[dict[str, Any]]
) -> dict[str, Any]:
    """POST /api/leads/create-or-update/multiple. Caller chunks at <= 500."""
    return _request_json(
        method="POST",
        path="/api/leads/create-or-update/multiple",
        api_key=api_key,
        json_payload={"leads": leads},
    )


# ── Stats / replies ───────────────────────────────────────────────────────


def get_campaign_stats_by_date(
    api_key: str, campaign_id: int | str
) -> dict[str, Any]:
    """GET /api/campaigns/{id}/line-area-chart-stats."""
    return _request_json(
        method="GET",
        path=f"/api/campaigns/{campaign_id}/line-area-chart-stats",
        api_key=api_key,
    )


def list_replies(
    api_key: str,
    *,
    campaign_id: int | str | None = None,
    page: int = 1,
    per_page: int = 100,
) -> dict[str, Any]:
    """GET /api/replies. Used by reconciliation."""
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if campaign_id is not None:
        params["campaign_id"] = campaign_id
    return _request_json(
        method="GET", path="/api/replies", api_key=api_key, params=params
    )


def get_reply(api_key: str, reply_id: int | str) -> dict[str, Any]:
    """GET /api/replies/{id} — the FULL canonical shape, not the trimmed MCP one."""
    return _request_json(
        method="GET", path=f"/api/replies/{reply_id}", api_key=api_key
    )


def list_campaigns(
    api_key: str,
    *,
    page: int = 1,
    per_page: int = 100,
    status: str | None = None,
) -> dict[str, Any]:
    """GET /api/campaigns."""
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if status is not None:
        params["status"] = status
    return _request_json(
        method="GET", path="/api/campaigns", api_key=api_key, params=params
    )


def list_webhook_event_types(api_key: str) -> dict[str, Any]:
    """GET /api/webhook-events/event-types — used at deploy / health check."""
    return _request_json(
        method="GET", path="/api/webhook-events/event-types", api_key=api_key
    )


# ── Tags (attach-only — full CRUD is out of scope) ────────────────────────


def attach_tags_to_campaigns(
    api_key: str,
    *,
    campaign_ids: list[int | str],
    tag_names: list[str],
) -> dict[str, Any]:
    """POST /api/tags/attach-to-campaigns.

    The adapter uses this to stamp the six-tuple onto an EB campaign as
    hqx:* tags. EmailBison has no campaign-level metadata field; tags are
    the only way to make webhook payloads echo our internal ids.
    """
    return _request_json(
        method="POST",
        path="/api/tags/attach-to-campaigns",
        api_key=api_key,
        json_payload={
            "campaign_ids": list(campaign_ids),
            "tag_names": list(tag_names),
        },
    )


__all__ = [
    "EMAILBISON_API_BASE",
    "EmailBisonProviderError",
    "create_campaign",
    "get_campaign",
    "update_campaign",
    "pause_campaign",
    "resume_campaign",
    "archive_campaign",
    "attach_leads",
    "detach_leads",
    "attach_sender_emails",
    "upsert_lead",
    "bulk_upsert_leads",
    "get_campaign_stats_by_date",
    "list_replies",
    "get_reply",
    "list_campaigns",
    "list_webhook_event_types",
    "attach_tags_to_campaigns",
]
