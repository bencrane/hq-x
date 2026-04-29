"""Vapi-orchestrated outbound calls API.

Distinct surface from ``app/routers/outbound_calls.py`` (Twilio-driven).
Here Vapi places the leg — the local call_logs row is keyed on
``vapi_call_id``, no Twilio TwiML callback is wired.

Required: ``Idempotency-Key`` header on POST. The service uses it to
dedupe replays so a network retry never doubles the Vapi charge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key
from app.services import vapi_calls as vapi_calls_svc

router = APIRouter(
    prefix="/api/brands/{brand_id}/voice/calls",
    tags=["vapi-calls"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VapiCallCreateRequest(BaseModel):
    assistant_id: UUID
    voice_phone_number_id: UUID
    customer_number: str
    customer_name: str | None = None
    customer_external_id: str | None = None
    metadata: dict[str, Any] | None = None
    partner_id: UUID | None = None
    campaign_id: UUID | None = None
    assistant_overrides: dict[str, Any] | None = None
    model_config = {"extra": "forbid"}


class VapiCallUpdateRequest(BaseModel):
    name: str | None = None
    model_config = {"extra": "forbid"}


_CALL_LOG_COLS = vapi_calls_svc._CALL_LOG_COLS


def _call_log_row(r: tuple) -> dict[str, Any]:
    return dict(zip(_CALL_LOG_COLS, r, strict=True))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_call(
    brand_id: UUID,
    body: VapiCallCreateRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={"error": "idempotency_key_required"},
        )
    api_key = vapi_key()
    try:
        result = await vapi_calls_svc.initiate_vapi_call(
            api_key=api_key,
            brand_id=brand_id,
            assistant_id=body.assistant_id,
            voice_phone_number_id=body.voice_phone_number_id,
            customer_number=body.customer_number,
            customer_name=body.customer_name,
            customer_external_id=body.customer_external_id,
            metadata=body.metadata,
            partner_id=body.partner_id,
            campaign_id=body.campaign_id,
            assistant_overrides=body.assistant_overrides,
            idempotency_key=idempotency_key,
        )
    except vapi_calls_svc.VapiOutboundValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": exc.error_key, "message": str(exc)},
        ) from exc
    except VapiProviderError as exc:
        raise_vapi_error("create_call", exc)
    return result


@router.get("")
async def list_calls(
    brand_id: UUID,
    assistant_id: UUID | None = None,
    campaign_id: UUID | None = None,
    partner_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200, ge=1),
    before: datetime | None = None,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List call_logs rows that have a Vapi call id, with cursor pagination on created_at."""
    where = ["brand_id = %s", "vapi_call_id IS NOT NULL", "deleted_at IS NULL"]
    params: list[Any] = [str(brand_id)]
    if assistant_id is not None:
        where.append("voice_assistant_id = %s")
        params.append(str(assistant_id))
    if campaign_id is not None:
        where.append("campaign_id = %s")
        params.append(str(campaign_id))
    if partner_id is not None:
        where.append("partner_id = %s")
        params.append(str(partner_id))
    if status_filter is not None:
        where.append("status = %s")
        params.append(status_filter)
    if before is not None:
        where.append("created_at < %s")
        params.append(before)
    params.append(limit)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_CALL_LOG_COLS)}
                FROM call_logs
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = await cur.fetchall()
    return [_call_log_row(r) for r in rows]


async def _load_call(brand_id: UUID, call_log_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_CALL_LOG_COLS)}
                FROM call_logs
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(call_log_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "call_not_found"})
    log = _call_log_row(row)
    if not log["vapi_call_id"]:
        raise HTTPException(status_code=404, detail={"error": "call_has_no_vapi_id"})
    return log


@router.get("/{call_log_id}")
async def get_call(
    brand_id: UUID,
    call_log_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    log = await _load_call(brand_id, call_log_id)
    api_key = vapi_key()
    try:
        vapi_view = vapi_client.get_call(api_key, log["vapi_call_id"])
    except VapiProviderError as exc:
        raise_vapi_error("get_call", exc)
    return {"local": log, "vapi": vapi_view}


@router.patch("/{call_log_id}")
async def update_call(
    brand_id: UUID,
    call_log_id: UUID,
    body: VapiCallUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    log = await _load_call(brand_id, call_log_id)
    api_key = vapi_key()
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        result = vapi_client.update_call(api_key, log["vapi_call_id"], name=fields.get("name"))
    except VapiProviderError as exc:
        raise_vapi_error("update_call", exc)
    return {"local": log, "vapi": result}


@router.post("/{call_log_id}/end")
async def end_call(
    brand_id: UUID,
    call_log_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Forcibly hang up the live call by issuing DELETE /call/{id} on Vapi.

    DESTRUCTIVE: Vapi's DELETE removes the Vapi-side call record entirely.
    Transcript / recording references on Vapi die asynchronously after this
    returns. Our local call_logs row is preserved with status='ended' and
    ended_at=NOW() — but if you need the transcript or cost data, retrieve
    it BEFORE calling this endpoint.

    Vapi's ``end-of-call-report`` webhook may still fire after this call and
    remains the source of truth for cost/transcript backfill — handled by
    app/routers/vapi_webhooks.py.
    """
    log = await _load_call(brand_id, call_log_id)
    api_key = vapi_key()
    try:
        result = vapi_client.delete_call(api_key, log["vapi_call_id"])
    except VapiProviderError as exc:
        raise_vapi_error("delete_call", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE call_logs
                SET status = 'ended',
                    ended_at = COALESCE(ended_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s AND brand_id = %s
                RETURNING {", ".join(_CALL_LOG_COLS)}
                """,
                (str(call_log_id), str(brand_id)),
            )
            updated = await cur.fetchone()
        await conn.commit()

    return {
        "local": _call_log_row(updated) if updated else log,
        "vapi": result,
    }
