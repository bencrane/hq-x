"""Internal EmailBison reconciliation router.

Coverage doc §4b prescribes "option 2: webhook + periodic reconciliation
pull." These endpoints are the second half: a synthetic-event injector
that walks ``GET /api/replies`` for each active step and projects any
replies the webhook delivery missed.

Both endpoints are protected by the same ``verify_trigger_secret``
dependency the scheduler uses, so the existing scheduler / Trigger.dev
fan-out can call them without bespoke auth.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.trigger_secret import verify_trigger_secret
from app.config import settings
from app.db import get_db_connection
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError
from app.webhooks import storage as webhook_storage
from app.webhooks.emailbison_processor import project_emailbison_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/emailbison", tags=["internal"])


class ReconcileRequest(BaseModel):
    since: datetime | None = None
    model_config = {"extra": "forbid"}


class ReconcileResponse(BaseModel):
    step_count: int
    synthesized_events: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class BackfillResponse(BaseModel):
    backfilled: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


def _api_key() -> str:
    if not settings.EMAILBISON_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "emailbison_not_configured",
                "message": "EMAILBISON_API_KEY not set",
            },
        )
    return settings.EMAILBISON_API_KEY


async def _list_active_steps_for_channel_campaign(
    channel_campaign_id: UUID,
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.external_provider_id, s.status,
                       s.organization_id
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.channel_campaign_id = %s
                  AND cc.channel = 'email'
                  AND cc.provider = 'emailbison'
                  AND s.external_provider_id IS NOT NULL
                  AND s.status NOT IN ('cancelled', 'archived', 'failed')
                ORDER BY s.step_order
                """,
                (str(channel_campaign_id),),
            )
            rows = await cur.fetchall()
    return [
        {
            "step_id": r[0],
            "external_provider_id": r[1],
            "status": r[2],
            "organization_id": r[3],
        }
        for r in rows
    ]


async def _get_step_for_backfill(
    step_id: UUID,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.external_provider_id, cc.channel, cc.provider
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.id = %s
                LIMIT 1
                """,
                (str(step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "external_provider_id": row[1],
        "channel": row[2],
        "provider": row[3],
    }


async def _ingest_synthetic_event(
    *,
    provider_slug: str,
    event_key: str,
    raw_event_name: str,
    payload: dict[str, Any],
) -> tuple[UUID, bool]:
    """Insert a synthesized webhook_events row and return (id, was_new)."""
    try:
        event_id = await webhook_storage.insert_emailbison_event(
            event_key=event_key,
            event_type=raw_event_name,
            payload=payload,
        )
        return event_id, True
    except webhook_storage.DuplicateEventError:
        return uuid4(), False


def _synthesize_reply_payload(
    *, reply: dict[str, Any], step_external_id: str
) -> dict[str, Any]:
    """Wrap a /api/replies entry in the lead_replied envelope shape."""
    reply_id = reply.get("id")
    return {
        "event": {
            "type": "LEAD_REPLIED",
            "name": "Lead Replied (synthesized)",
            "workspace_id": reply.get("workspace_id"),
        },
        "data": {
            "reply": reply,
            "campaign": {"id": int(step_external_id), "tags": []}
            if step_external_id.isdigit()
            else {"id": step_external_id, "tags": []},
            "scheduled_email": reply.get("scheduled_email") or {},
            "lead": reply.get("lead") or {},
            "sender_email": reply.get("sender_email") or {},
            "campaign_event": {"type": "replied", "id": reply_id},
        },
        "_synthesized": True,
    }


@router.post("/reconcile/{channel_campaign_id}", response_model=ReconcileResponse,
             dependencies=[Depends(verify_trigger_secret)])
async def reconcile_channel_campaign(
    channel_campaign_id: UUID, body: ReconcileRequest | None = None
) -> ReconcileResponse:
    api_key = _api_key()
    steps = await _list_active_steps_for_channel_campaign(channel_campaign_id)
    synthesized = 0
    errors: list[dict[str, Any]] = []

    for step in steps:
        eb_campaign_id = step["external_provider_id"]
        # Snapshot stats — store under metadata for later diff.
        try:
            stats = eb_client.get_campaign_stats_by_date(api_key, eb_campaign_id)
            await _record_recon_stats_snapshot(
                step_id=step["step_id"], stats=stats
            )
        except EmailBisonProviderError as exc:
            errors.append({"step_id": str(step["step_id"]), "error": str(exc)})

        # Walk replies and synthesize events for any that haven't been
        # projected yet.
        page = 1
        while True:
            try:
                response = eb_client.list_replies(
                    api_key,
                    campaign_id=eb_campaign_id,
                    page=page,
                    per_page=100,
                )
            except EmailBisonProviderError as exc:
                errors.append(
                    {"step_id": str(step["step_id"]), "error": str(exc)}
                )
                break
            replies = (
                response.get("replies")
                or response.get("data")
                or []
            )
            if not isinstance(replies, list) or not replies:
                break
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_id = reply.get("id")
                if reply_id is None:
                    continue
                event_key = f"recon:{eb_campaign_id}:reply:{reply_id}"
                payload = _synthesize_reply_payload(
                    reply=reply, step_external_id=str(eb_campaign_id)
                )
                event_id, was_new = await _ingest_synthetic_event(
                    provider_slug="emailbison",
                    event_key=event_key,
                    raw_event_name="LEAD_REPLIED",
                    payload=payload,
                )
                if was_new:
                    synthesized += 1
                    try:
                        await project_emailbison_event(
                            webhook_event_id=event_id, payload=payload
                        )
                    except Exception as exc:  # pragma: no cover
                        logger.exception(
                            "synthetic projection failed reply=%s", reply_id
                        )
                        errors.append(
                            {
                                "step_id": str(step["step_id"]),
                                "reply_id": reply_id,
                                "error": str(exc)[:300],
                            }
                        )
            has_more = response.get("has_more")
            next_page = response.get("next_page")
            if has_more and next_page:
                page = int(next_page)
            else:
                break

    return ReconcileResponse(
        step_count=len(steps),
        synthesized_events=synthesized,
        errors=errors,
    )


async def _record_recon_stats_snapshot(
    *, step_id: UUID, stats: dict[str, Any]
) -> None:
    from psycopg.types.json import Jsonb

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps
                SET external_provider_metadata =
                        COALESCE(external_provider_metadata, '{}'::jsonb)
                        || jsonb_build_object('last_recon', %s::jsonb),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (Jsonb(stats), str(step_id)),
            )


@router.post("/backfill/{channel_campaign_step_id}",
             response_model=BackfillResponse,
             dependencies=[Depends(verify_trigger_secret)])
async def backfill_step(channel_campaign_step_id: UUID) -> BackfillResponse:
    api_key = _api_key()
    step = await _get_step_for_backfill(channel_campaign_step_id)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "step_not_found"},
        )
    if step["channel"] != "email" or step["provider"] != "emailbison":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "not_an_emailbison_step"},
        )
    if not step["external_provider_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "step_not_activated"},
        )

    eb_campaign_id = step["external_provider_id"]
    backfilled = 0
    errors: list[dict[str, Any]] = []

    # Stats snapshot once.
    try:
        stats = eb_client.get_campaign_stats_by_date(api_key, eb_campaign_id)
        await _record_recon_stats_snapshot(
            step_id=channel_campaign_step_id, stats=stats
        )
    except EmailBisonProviderError as exc:
        errors.append({"error": str(exc)})

    page = 1
    while True:
        try:
            response = eb_client.list_replies(
                api_key,
                campaign_id=eb_campaign_id,
                page=page,
                per_page=100,
            )
        except EmailBisonProviderError as exc:
            errors.append({"error": str(exc)})
            break
        replies = (
            response.get("replies")
            or response.get("data")
            or []
        )
        if not isinstance(replies, list) or not replies:
            break
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            reply_id = reply.get("id")
            if reply_id is None:
                continue
            event_key = f"backfill:{eb_campaign_id}:reply:{reply_id}"
            payload = _synthesize_reply_payload(
                reply=reply, step_external_id=str(eb_campaign_id)
            )
            event_id, was_new = await _ingest_synthetic_event(
                provider_slug="emailbison",
                event_key=event_key,
                raw_event_name="LEAD_REPLIED",
                payload=payload,
            )
            if was_new:
                backfilled += 1
                try:
                    await project_emailbison_event(
                        webhook_event_id=event_id, payload=payload
                    )
                except Exception as exc:  # pragma: no cover
                    logger.exception(
                        "backfill projection failed reply=%s", reply_id
                    )
                    errors.append(
                        {"reply_id": reply_id, "error": str(exc)[:300]}
                    )
        has_more = response.get("has_more")
        next_page = response.get("next_page")
        if has_more and next_page:
            page = int(next_page)
        else:
            break

    return BackfillResponse(backfilled=backfilled, errors=errors)
