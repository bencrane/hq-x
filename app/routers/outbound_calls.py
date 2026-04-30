"""Outbound calls REST + TwiML endpoints.

Brand-axis port of OEX ``routers/outbound_calls.py``. References to the
``voicemail_drops`` table are dropped (table not ported); the callback runner
relies on ``voicemail_audio_url`` plus Vapi-driven dynamic voicemail.

Critical:
  - ``voice_sessions`` writes redirected to ``call_logs``.
  - The ``_MAX_CONNECT_LOOPS = 5`` dial-loop guard is preserved.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.twiml import (
    build_hangup_response,
    build_human_answered_response,
    build_outbound_connect_response,
    build_vapi_sip_transfer_response,
    build_voicemail_drop_response,
)
from app.services import brands as brands_svc
from app.services.outbound_calls import _api_base_url, initiate_outbound_call

logger = logging.getLogger(__name__)

router = APIRouter(tags=["outbound-calls"])

_MAX_CONNECT_LOOPS = 5


class OutboundCallRequest(BaseModel):
    to: str
    from_number: str
    greeting_text: str | None = None
    voicemail_text: str | None = None
    voicemail_audio_url: str | None = None
    human_message_text: str | None = None
    record: bool = False
    timeout: int = Field(default=30, ge=5, le=600)
    partner_id: UUID | None = None
    channel_campaign_id: UUID | None = None
    campaign_lead_id: str | None = None
    amd_strategy: str | None = None
    vapi_assistant_id: str | None = None
    vapi_sip_uri: str | None = None
    model_config = {"extra": "forbid"}


class OutboundCallResponse(BaseModel):
    call_sid: str
    call_log_id: str
    status: str
    direction: str
    from_number: str
    to: str


_CONFIG_COLS = [
    "id", "brand_id", "call_log_id", "twiml_token",
    "greeting_text", "voicemail_text", "voicemail_audio_url",
    "human_message_text", "voice", "language",
    "amd_strategy", "vapi_assistant_id", "vapi_sip_uri",
    "campaign_voice_config", "from_number",
]


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


async def _validate_twiml_token(call_log_id: UUID, token: str | None) -> dict[str, Any]:
    if not token:
        raise HTTPException(status_code=403, detail="Missing token")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_CONFIG_COLS)}
                FROM outbound_call_configs
                WHERE call_log_id = %s
                LIMIT 1
                """,
                (str(call_log_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Config not found")
    config = dict(zip(_CONFIG_COLS, row, strict=True))
    if config.get("twiml_token") != token:
        raise HTTPException(status_code=403, detail="Invalid token")
    return config


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/brands/{brand_id}/outbound-calls",
    response_model=OutboundCallResponse,
)
async def create_outbound_call(
    brand_id: UUID,
    body: OutboundCallRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> OutboundCallResponse:
    if body.voicemail_text and body.voicemail_audio_url:
        raise HTTPException(
            status_code=400,
            detail="Cannot provide both voicemail_text and voicemail_audio_url",
        )
    creds = await _resolve_creds(brand_id)

    try:
        result = await initiate_outbound_call(
            brand_id=brand_id,
            account_sid=creds.account_sid,
            auth_token=creds.auth_token,
            to=body.to,
            from_number=body.from_number,
            greeting_text=body.greeting_text,
            voicemail_text=body.voicemail_text,
            voicemail_audio_url=body.voicemail_audio_url,
            human_message_text=body.human_message_text,
            record=body.record,
            timeout=body.timeout,
            partner_id=body.partner_id,
            channel_campaign_id=body.channel_campaign_id,
            campaign_lead_id=body.campaign_lead_id,
            amd_strategy=body.amd_strategy,
            vapi_assistant_id=body.vapi_assistant_id,
            vapi_sip_uri=body.vapi_sip_uri,
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

    return OutboundCallResponse(**result)


@router.get("/api/brands/{brand_id}/outbound-calls/{call_sid}")
async def get_outbound_call(
    brand_id: UUID,
    call_sid: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    cols = [
        "id", "brand_id", "twilio_call_sid", "direction", "call_type",
        "customer_number", "from_number", "status", "outcome",
        "started_at", "ended_at", "duration_seconds",
        "amd_result", "business_disposition", "dial_action_status",
    ]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM call_logs
                WHERE twilio_call_sid = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (call_sid, str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return dict(zip(cols, row, strict=True))


# ---------------------------------------------------------------------------
# TwiML endpoints (called by Twilio, token-validated; no auth dep).
#
# These are NOT under /api/brands/{brand_id} because Twilio holds them as
# fixed URLs after the call is queued. They take call_log_id and validate
# the per-call token instead.
# ---------------------------------------------------------------------------


@router.post("/api/voice/outbound/twiml/connect/{call_log_id}")
async def twiml_connect(
    call_log_id: UUID,
    token: str | None = Query(default=None),
    loop_count: int = Query(default=0),
) -> Response:
    config = await _validate_twiml_token(call_log_id, token)

    if loop_count >= _MAX_CONNECT_LOOPS:
        twiml = build_human_answered_response(
            message_text=config.get("human_message_text") or "Hello, we were trying to reach you. Goodbye.",
            voice=config.get("voice", "Polly.Matthew-Generative"),
            language=config.get("language", "en-US"),
        )
        return Response(content=twiml, media_type="text/xml")

    try:
        api_base = _api_base_url()
    except ValueError:
        twiml = build_hangup_response()
        return Response(content=twiml, media_type="text/xml")

    next_loop = loop_count + 1
    redirect_url = (
        f"{api_base}/api/voice/outbound/twiml/connect/{call_log_id}"
        f"?token={token}&loop_count={next_loop}"
    )

    twiml = build_outbound_connect_response(
        greeting_text=config.get("greeting_text"),
        voice=config.get("voice", "Polly.Matthew-Generative"),
        language=config.get("language", "en-US"),
        redirect_url=redirect_url,
    )
    return Response(content=twiml, media_type="text/xml")


@router.post("/api/voice/outbound/twiml/voicemail-drop/{call_log_id}")
async def twiml_voicemail_drop(
    call_log_id: UUID,
    token: str | None = Query(default=None),
) -> Response:
    config = await _validate_twiml_token(call_log_id, token)
    voicemail_text = config.get("voicemail_text")
    voicemail_audio_url = config.get("voicemail_audio_url")
    if not voicemail_text and not voicemail_audio_url:
        twiml = build_hangup_response("We're sorry, goodbye.")
        return Response(content=twiml, media_type="text/xml")
    twiml = build_voicemail_drop_response(
        message_text=voicemail_text,
        audio_url=voicemail_audio_url,
        voice=config.get("voice", "Polly.Matthew-Generative"),
        language=config.get("language", "en-US"),
    )
    return Response(content=twiml, media_type="text/xml")


@router.post("/api/voice/outbound/twiml/human-answered/{call_log_id}")
async def twiml_human_answered(
    call_log_id: UUID,
    token: str | None = Query(default=None),
) -> Response:
    config = await _validate_twiml_token(call_log_id, token)
    human_message = config.get("human_message_text")
    if not human_message:
        twiml = build_hangup_response()
        return Response(content=twiml, media_type="text/xml")
    twiml = build_human_answered_response(
        message_text=human_message,
        voice=config.get("voice", "Polly.Matthew-Generative"),
        language=config.get("language", "en-US"),
    )
    return Response(content=twiml, media_type="text/xml")


@router.post("/api/voice/outbound/twiml/vapi-sip-forward/{call_log_id}")
async def twiml_vapi_sip_forward(
    call_log_id: UUID,
    token: str | None = Query(default=None),
) -> Response:
    config = await _validate_twiml_token(call_log_id, token)
    vapi_sip_uri = config.get("vapi_sip_uri")
    if not vapi_sip_uri:
        twiml = build_hangup_response()
        return Response(content=twiml, media_type="text/xml")
    caller_id = config.get("from_number") or ""
    sip_headers: dict[str, str] = {}
    if config.get("vapi_assistant_id"):
        sip_headers["X-Vapi-Assistant-Id"] = config["vapi_assistant_id"]
    if config.get("brand_id"):
        sip_headers["X-Brand-Id"] = str(config["brand_id"])
    twiml = build_vapi_sip_transfer_response(
        sip_uri=vapi_sip_uri,
        caller_id=caller_id,
        sip_headers=sip_headers if sip_headers else None,
    )
    return Response(content=twiml, media_type="text/xml")


@router.post("/api/voice/outbound/twiml/ai-voicemail-drop/{call_log_id}")
async def twiml_ai_voicemail_drop(
    call_log_id: UUID,
    token: str | None = Query(default=None),
) -> Response:
    """Play a voicemail audio URL stored on the per-call config.

    OEX previously looked up a ``voicemail_drops`` row by id; that table is
    not ported (Vapi handles voicemail TTS dynamically). We fall back to the
    per-call ``voicemail_audio_url`` saved on ``outbound_call_configs``.
    """
    config = await _validate_twiml_token(call_log_id, token)
    audio_url = config.get("voicemail_audio_url")
    if not audio_url:
        twiml = build_hangup_response("We're sorry, goodbye.")
        return Response(content=twiml, media_type="text/xml")
    twiml = build_voicemail_drop_response(audio_url=audio_url)
    return Response(content=twiml, media_type="text/xml")
