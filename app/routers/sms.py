"""SMS REST API.

Send, get, list. Suppression list management is here too. The actual
inbound + status-callback consumer lives on the Twilio webhook receiver
(routers/twilio_webhooks.py).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.providers.twilio._http import TwilioProviderError
from app.services import brands as brands_svc
from app.services.sms import (
    SmsSuppressedError,
    add_suppression,
    send_sms,
)

router = APIRouter(prefix="/api/brands/{brand_id}/sms", tags=["sms"])


class SendSmsRequest(BaseModel):
    to: str
    body: str | None = None
    from_number: str | None = None
    messaging_service_sid: str | None = None
    media_url: list[str] | None = None
    partner_id: UUID | None = None
    channel_campaign_id: UUID | None = None
    model_config = {"extra": "forbid"}


class SendSmsResponse(BaseModel):
    message_sid: str
    status: str
    direction: str
    from_number: str
    to: str


class SuppressionRequest(BaseModel):
    phone_number: str
    reason: str = "manual"
    notes: str | None = None
    model_config = {"extra": "forbid"}


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


@router.post("", response_model=SendSmsResponse)
async def send_sms_endpoint(
    brand_id: UUID,
    body: SendSmsRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> SendSmsResponse:
    if not body.body and not body.media_url:
        raise HTTPException(
            status_code=400,
            detail={"error": "must provide body or media_url"},
        )
    if body.from_number and body.messaging_service_sid:
        raise HTTPException(
            status_code=400,
            detail={"error": "cannot provide both from_number and messaging_service_sid"},
        )

    creds = await _resolve_creds(brand_id)

    from_number = body.from_number
    messaging_service_sid = body.messaging_service_sid
    if not from_number and not messaging_service_sid:
        brand = await brands_svc.get_brand(brand_id)
        if brand and brand.twilio_messaging_service_sid:
            messaging_service_sid = brand.twilio_messaging_service_sid
    if not from_number and not messaging_service_sid:
        raise HTTPException(
            status_code=400,
            detail={"error": "no sender configured (no from_number, messaging_service_sid, or brand default)"},
        )

    try:
        result = await send_sms(
            brand_id=brand_id,
            account_sid=creds.account_sid,
            auth_token=creds.auth_token,
            to=body.to,
            body=body.body,
            from_number=from_number,
            messaging_service_sid=messaging_service_sid,
            media_url=body.media_url,
            partner_id=body.partner_id,
            channel_campaign_id=body.channel_campaign_id,
        )
    except SmsSuppressedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "phone_number_suppressed", "message": str(exc)},
        ) from exc
    except TwilioProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "twilio_error", "message": str(exc)},
        ) from exc

    return SendSmsResponse(**result)


@router.get("")
async def list_sms(
    brand_id: UUID,
    direction: str | None = Query(default=None),
    sms_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, message_sid, direction, from_number, to_number,
               body, status, error_code, created_at, updated_at
        FROM sms_messages
        WHERE brand_id = %s
    """
    params: list[Any] = [str(brand_id)]
    if direction:
        sql += " AND direction = %s"
        params.append(direction)
    if sms_status:
        sql += " AND status = %s"
        params.append(sms_status)
    sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


# ---------------------------------------------------------------------------
# Suppression management (drift fix §7.3) — registered BEFORE /{message_sid}
# so the static segments don't get captured by the message-sid wildcard.
# ---------------------------------------------------------------------------


@router.post("/suppressions", status_code=status.HTTP_201_CREATED)
async def create_suppression(
    brand_id: UUID,
    body: SuppressionRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if body.reason not in ("stop_keyword", "manual", "bounce"):
        raise HTTPException(status_code=400, detail={"error": "invalid reason"})
    await add_suppression(
        brand_id, body.phone_number,
        reason=body.reason, notes=body.notes,
    )
    return {"phone_number": body.phone_number, "reason": body.reason}


@router.get("/suppressions")
async def list_suppressions(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, phone_number, reason, notes, created_at
                FROM sms_suppressions
                WHERE brand_id = %s
                ORDER BY created_at DESC
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


@router.delete("/suppressions/{phone_number:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suppression(
    brand_id: UUID,
    phone_number: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM sms_suppressions
                WHERE brand_id = %s AND phone_number = %s
                """,
                (str(brand_id), phone_number),
            )
        await conn.commit()


# Wildcard message_sid lookup — must be registered LAST so it doesn't
# capture /suppressions or other static segments.
@router.get("/{message_sid}")
async def get_sms(
    brand_id: UUID,
    message_sid: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, message_sid, account_sid, direction,
                       from_number, to_number, body, status, error_code,
                       num_segments, num_media, created_at, updated_at
                FROM sms_messages
                WHERE message_sid = %s AND brand_id = %s
                """,
                (message_sid, str(brand_id)),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "sms_not_found"})
    return dict(zip(cols, row, strict=True))
