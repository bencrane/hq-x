"""Voice CRUD + call-control surface, brand-axis.

Brand-axis port of OEX ``routers/voice.py``. Per directive §3 the
``GET /api/voice/token`` endpoint is **dropped** (Voice Access Token deferred).
Transcription / live transcription / Intelligence-Service endpoints are also
deferred (call_transcripts + live_transcription_utterances tables not ported).

What stays:
  - Disposition writes onto ``call_logs.business_disposition``.
  - Call actions (hangup/redirect/hold/unhold) via Twilio update_call.
  - Session queries against ``call_logs`` (folded ``voice_sessions``).
  - Transfer-territory CRUD against ``transfer_territories``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.db import get_db_connection
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import update_call
from app.services import brands as brands_svc

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/brands/{brand_id}/voice", tags=["voice"])


VALID_DISPOSITIONS = {
    "meeting_booked", "callback_scheduled", "follow_up_needed",
    "left_voicemail", "not_interested", "wrong_number",
    "no_answer", "busy", "gatekeeper", "qualified",
    "disqualified", "do_not_call", "other",
}
VALID_ACTIONS = {"hangup", "redirect", "hold", "unhold"}


class DispositionRequest(BaseModel):
    disposition: str
    notes: str | None = None
    model_config = {"extra": "forbid"}


class DispositionResponse(BaseModel):
    call_sid: str
    business_disposition: str


class CallActionRequest(BaseModel):
    action: str
    twiml: str | None = None
    url: str | None = None
    model_config = {"extra": "forbid"}


class TransferTerritoryCreate(BaseModel):
    name: str
    channel_campaign_id: UUID | None = None
    partner_id: UUID | None = None
    rules: dict[str, Any] | None = None
    destination_phone: str
    destination_label: str | None = None
    priority: int = 0
    active: bool = True
    model_config = {"extra": "forbid"}


class TransferTerritoryUpdate(BaseModel):
    name: str | None = None
    rules: dict[str, Any] | None = None
    destination_phone: str | None = None
    destination_label: str | None = None
    priority: int | None = None
    active: bool | None = None
    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_creds(brand_id: UUID) -> brands_svc.BrandTwilioCreds:
    try:
        creds = await brands_svc.get_twilio_creds(brand_id)
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "Brand has no Twilio credentials configured"},
        )
    return creds


_CALL_LOG_COLS = [
    "id", "brand_id", "partner_id", "channel_campaign_id",
    "voice_assistant_id", "voice_phone_number_id",
    "vapi_call_id", "twilio_call_sid",
    "direction", "call_type",
    "customer_number", "from_number", "status", "ended_reason", "outcome",
    "amd_result", "business_disposition", "dial_action_status",
    "started_at", "ended_at", "duration_seconds",
    "recording_url", "recording_sid",
    "metadata", "created_at", "updated_at",
]


def _row_to_call_log(row: tuple) -> dict[str, Any]:
    return dict(zip(_CALL_LOG_COLS, row, strict=True))


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------


@router.post("/sessions/{call_sid}/disposition", response_model=DispositionResponse)
async def set_disposition(
    brand_id: UUID,
    call_sid: str,
    body: DispositionRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> DispositionResponse:
    if body.disposition not in VALID_DISPOSITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid disposition. Must be one of: {', '.join(sorted(VALID_DISPOSITIONS))}",
        )

    update_metadata = body.notes is not None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if update_metadata:
                await cur.execute(
                    """
                    UPDATE call_logs
                    SET business_disposition = %s,
                        metadata = COALESCE(metadata, '{}'::jsonb)
                                   || jsonb_build_object('disposition_notes', %s::text),
                        updated_at = NOW()
                    WHERE twilio_call_sid = %s AND brand_id = %s AND deleted_at IS NULL
                    RETURNING twilio_call_sid, business_disposition
                    """,
                    (body.disposition, body.notes, call_sid, str(brand_id)),
                )
            else:
                await cur.execute(
                    """
                    UPDATE call_logs
                    SET business_disposition = %s, updated_at = NOW()
                    WHERE twilio_call_sid = %s AND brand_id = %s AND deleted_at IS NULL
                    RETURNING twilio_call_sid, business_disposition
                    """,
                    (body.disposition, call_sid, str(brand_id)),
                )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="Voice session not found")
    return DispositionResponse(call_sid=row[0], business_disposition=row[1])


# ---------------------------------------------------------------------------
# Call actions
# ---------------------------------------------------------------------------


@router.post("/sessions/{call_sid}/action")
async def call_action(
    brand_id: UUID,
    call_sid: str,
    body: CallActionRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if body.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action. Must be one of: {', '.join(sorted(VALID_ACTIONS))}",
        )
    creds = await _resolve_creds(brand_id)

    # Validate session exists and belongs to this brand
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM call_logs
                WHERE twilio_call_sid = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (call_sid, str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Voice session not found")

    try:
        if body.action == "hangup":
            update_call(
                creds.account_sid, creds.auth_token,
                call_sid=call_sid, status="completed",
            )
        elif body.action == "redirect":
            if not body.url and not body.twiml:
                raise HTTPException(
                    status_code=400, detail="redirect requires url or twiml",
                )
            update_call(
                creds.account_sid, creds.auth_token,
                call_sid=call_sid, url=body.url, twiml=body.twiml,
            )
        elif body.action == "hold":
            hold_twiml = (
                '<Response><Say>Please hold.</Say>'
                '<Play loop="0">http://com.twilio.sounds.music.s3.amazonaws.com/ClockworkWaltz.mp3</Play>'
                '</Response>'
            )
            update_call(
                creds.account_sid, creds.auth_token,
                call_sid=call_sid, twiml=hold_twiml,
            )
        elif body.action == "unhold":
            api_base = (settings.HQX_API_BASE_URL or "").__str__().rstrip("/")
            if not api_base:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "HQX_API_BASE_URL not configured"},
                )
            webhook_url = f"{api_base}/api/webhooks/twilio/{brand_id}"
            update_call(
                creds.account_sid, creds.auth_token,
                call_sid=call_sid, url=webhook_url,
            )
    except TwilioProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "type": "provider_error",
                "provider": "twilio",
                "retryable": exc.retryable,
                "message": str(exc),
            },
        ) from exc

    return {"call_sid": call_sid, "action": body.action, "status": "ok"}


# ---------------------------------------------------------------------------
# Session queries (against call_logs)
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    brand_id: UUID,
    partner_id: UUID | None = None,
    channel_campaign_id: UUID | None = None,
    direction: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    disposition: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    where_parts = ["brand_id = %s", "deleted_at IS NULL"]
    values: list[Any] = [str(brand_id)]
    if partner_id:
        where_parts.append("partner_id = %s")
        values.append(str(partner_id))
    if channel_campaign_id:
        where_parts.append("channel_campaign_id = %s")
        values.append(str(channel_campaign_id))
    if direction:
        where_parts.append("direction = %s")
        values.append(direction)
    if status_filter:
        where_parts.append("status = %s")
        values.append(status_filter)
    if disposition:
        where_parts.append("business_disposition = %s")
        values.append(disposition)
    values.extend([limit, offset])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_CALL_LOG_COLS)}
                FROM call_logs
                WHERE {' AND '.join(where_parts)}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                values,
            )
            rows = await cur.fetchall()
    return [_row_to_call_log(r) for r in rows]


@router.get("/sessions/{call_sid}")
async def get_session(
    brand_id: UUID,
    call_sid: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_CALL_LOG_COLS)}
                FROM call_logs
                WHERE twilio_call_sid = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (call_sid, str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Voice session not found")
    return _row_to_call_log(row)


# ---------------------------------------------------------------------------
# Transfer territories CRUD
# ---------------------------------------------------------------------------


_TT_COLS = [
    "id", "brand_id", "partner_id", "channel_campaign_id",
    "name", "rules", "destination_phone", "destination_label",
    "priority", "active", "created_at", "updated_at",
]


def _row_to_tt(row: tuple) -> dict[str, Any]:
    return dict(zip(_TT_COLS, row, strict=True))


@router.post("/transfer-territories", status_code=status.HTTP_201_CREATED)
async def create_transfer_territory(
    brand_id: UUID,
    body: TransferTerritoryCreate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO transfer_territories (
                    brand_id, partner_id, channel_campaign_id,
                    name, rules, destination_phone, destination_label,
                    priority, active
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                RETURNING {', '.join(_TT_COLS)}
                """,
                (
                    str(brand_id),
                    str(body.partner_id) if body.partner_id else None,
                    str(body.channel_campaign_id) if body.channel_campaign_id else None,
                    body.name,
                    json.dumps(body.rules) if body.rules is not None else None,
                    body.destination_phone,
                    body.destination_label,
                    body.priority,
                    body.active,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_tt(row)


@router.get("/transfer-territories")
async def list_transfer_territories(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_TT_COLS)}
                FROM transfer_territories
                WHERE brand_id = %s AND deleted_at IS NULL
                ORDER BY priority DESC
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_tt(r) for r in rows]


@router.get("/transfer-territories/{territory_id}")
async def get_transfer_territory(
    brand_id: UUID,
    territory_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_TT_COLS)}
                FROM transfer_territories
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(territory_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer territory not found")
    return _row_to_tt(row)


@router.patch("/transfer-territories/{territory_id}")
async def update_transfer_territory(
    brand_id: UUID,
    territory_id: UUID,
    body: TransferTerritoryUpdate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    set_parts: list[str] = []
    values: list[Any] = []
    for k, v in updates.items():
        if k == "rules":
            set_parts.append("rules = %s::jsonb")
            values.append(json.dumps(v))
        else:
            set_parts.append(f"{k} = %s")
            values.append(v)
    set_parts.append("updated_at = NOW()")
    values.extend([str(territory_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE transfer_territories
                SET {', '.join(set_parts)}
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING {', '.join(_TT_COLS)}
                """,
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer territory not found")
    return _row_to_tt(row)


@router.delete("/transfer-territories/{territory_id}")
async def delete_transfer_territory(
    brand_id: UUID,
    territory_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE transfer_territories
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (str(territory_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer territory not found")
    return {"deleted": True, "id": str(territory_id)}
