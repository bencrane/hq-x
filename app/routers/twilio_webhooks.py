"""Twilio webhook receiver.

POST /api/webhooks/twilio/{brand_id} — voice + SMS + recording + dial-action.

Drift fixes:
  §7.2 — SMS status-callback events are mapped to sms_messages.status updates.
  §7.5 — Inbound SMS with STOP keyword inserts into sms_suppressions.

Folds OEX voice_sessions writes into call_logs (single source of truth).
Skips post-call Intelligence (transcription) and live transcription paths
(deferred per directive §10).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio.webhooks import validate_twilio_signature
from app.services import brands as brands_svc
from app.services import sms as sms_svc

router = APIRouter(prefix="/api/webhooks/twilio", tags=["webhooks-twilio"])
logger = logging.getLogger(__name__)

_TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


@dataclass
class TwilioEvent:
    event_type: str
    brand_id: str
    call_sid: str | None
    account_sid: str
    from_number: str | None
    to_number: str | None
    call_status: str | None
    call_duration: int | None
    direction: str | None
    sequence_number: int | None
    recording_sid: str | None
    recording_url: str | None
    recording_status: str | None
    recording_duration: int | None
    answered_by: str | None
    dial_call_status: str | None
    dial_call_sid: str | None
    dial_call_duration: int | None
    message_sid: str | None
    message_status: str | None
    message_body: str | None
    error_code: int | None
    raw_payload: dict[str, str]


def _detect_event_type(p: dict[str, str]) -> str:
    if p.get("RecordingSid") and p.get("RecordingStatus"):
        return "recording_status"
    if p.get("AnsweredBy"):
        return "amd_result"
    if p.get("DialCallStatus"):
        return "dial_action"
    if p.get("MessageSid") and p.get("MessageStatus"):
        return "message_status"
    if p.get("MessageSid") and p.get("Body") is not None and not p.get("MessageStatus"):
        return "inbound_message"
    if p.get("CallStatus"):
        return "call_status"
    return "unknown"


def _parse(brand_id: str, p: dict[str, str]) -> TwilioEvent:
    return TwilioEvent(
        event_type=_detect_event_type(p),
        brand_id=brand_id,
        call_sid=p.get("CallSid"),
        account_sid=p.get("AccountSid", ""),
        from_number=p.get("From"),
        to_number=p.get("To"),
        call_status=p.get("CallStatus"),
        call_duration=_safe_int(p.get("CallDuration")),
        direction=p.get("Direction"),
        sequence_number=_safe_int(p.get("SequenceNumber")),
        recording_sid=p.get("RecordingSid"),
        recording_url=p.get("RecordingUrl"),
        recording_status=p.get("RecordingStatus"),
        recording_duration=_safe_int(p.get("RecordingDuration")),
        answered_by=p.get("AnsweredBy"),
        dial_call_status=p.get("DialCallStatus"),
        dial_call_sid=p.get("DialCallSid"),
        dial_call_duration=_safe_int(p.get("DialCallDuration")),
        message_sid=p.get("MessageSid"),
        message_status=p.get("MessageStatus"),
        message_body=p.get("Body"),
        error_code=_safe_int(p.get("ErrorCode")),
        raw_payload=p,
    )


def _event_key(e: TwilioEvent) -> str:
    if e.event_type == "recording_status":
        return f"twilio:recording:{e.recording_sid}:{e.recording_status}"
    if e.event_type == "message_status":
        return f"twilio:msg-status:{e.message_sid}:{e.message_status}"
    if e.event_type == "inbound_message":
        return f"twilio:inbound-msg:{e.message_sid}"
    if e.event_type == "amd_result":
        return f"twilio:amd:{e.call_sid}"
    if e.event_type == "dial_action":
        return f"twilio:dial:{e.call_sid}:{e.dial_call_sid}:{e.dial_call_status}"
    if e.sequence_number is not None:
        return f"twilio:call:{e.call_sid}:{e.sequence_number}"
    return f"twilio:{e.event_type}:{e.call_sid}"


async def _persist_event_or_skip_duplicate(
    *, event_key: str, event_type: str, payload: dict[str, str], brand_id: str,
) -> bool:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO webhook_events (
                        provider_slug, event_key, event_type, status,
                        replay_count, payload, brand_id
                    )
                    VALUES ('twilio', %s, %s, 'processed', 0, %s::jsonb, %s)
                    """,
                    (event_key, event_type, json.dumps(payload), brand_id),
                )
            await conn.commit()
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            return False
        raise


def _reconstruct_url(request: Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", ""))
    path = request.url.path
    query = str(request.url.query) if request.url.query else ""
    return f"{proto}://{host}{path}" + (f"?{query}" if query else "")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_call_status(e: TwilioEvent) -> None:
    if not e.call_sid:
        return

    started_at_clause = (
        ", started_at = COALESCE(call_logs.started_at, NOW())"
        if e.call_status == "in-progress"
        else ""
    )
    ended_at_clause = (
        ", ended_at = COALESCE(call_logs.ended_at, NOW())"
        if e.call_status in _TERMINAL_CALL_STATUSES
        else ""
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # call_logs has no UNIQUE on twilio_call_sid; use UPDATE-then-INSERT pattern.
            await cur.execute(
                f"""
                UPDATE call_logs
                SET status = %s,
                    from_number = COALESCE(%s, from_number),
                    customer_number = COALESCE(%s, customer_number),
                    direction = COALESCE(%s, direction),
                    duration_seconds = COALESCE(%s, duration_seconds),
                    updated_at = NOW()
                    {started_at_clause}
                    {ended_at_clause}
                WHERE twilio_call_sid = %s AND brand_id = %s
                RETURNING id
                """,
                (
                    e.call_status or "queued",
                    e.from_number,
                    e.to_number,
                    e.direction,
                    e.call_duration,
                    e.call_sid,
                    e.brand_id,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                # No existing call_logs row — insert one.
                await cur.execute(
                    """
                    INSERT INTO call_logs (
                        brand_id, twilio_call_sid, status, direction,
                        from_number, customer_number, duration_seconds,
                        started_at, ended_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        e.brand_id, e.call_sid, e.call_status or "queued",
                        e.direction or "inbound",
                        e.from_number, e.to_number, e.call_duration,
                        None, None,  # started_at / ended_at filled by future events
                    ),
                )
        await conn.commit()


async def _handle_amd_result(e: TwilioEvent) -> None:
    if not e.call_sid or not e.answered_by:
        return
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE call_logs
                SET amd_result = %s, updated_at = NOW()
                WHERE twilio_call_sid = %s AND brand_id = %s
                """,
                (e.answered_by, e.call_sid, e.brand_id),
            )
        await conn.commit()


async def _handle_recording_status(e: TwilioEvent) -> None:
    if not e.call_sid or e.recording_status != "completed":
        return
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE call_logs
                SET recording_sid = %s, recording_url = %s, updated_at = NOW()
                WHERE twilio_call_sid = %s AND brand_id = %s
                """,
                (e.recording_sid, e.recording_url, e.call_sid, e.brand_id),
            )
        await conn.commit()


async def _handle_dial_action(e: TwilioEvent) -> None:
    if not e.call_sid:
        return
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE call_logs
                SET dial_action_status = %s, updated_at = NOW()
                WHERE twilio_call_sid = %s AND brand_id = %s
                """,
                (e.dial_call_status, e.call_sid, e.brand_id),
            )
        await conn.commit()


async def _handle_message_status(e: TwilioEvent) -> None:
    """Drift fix §7.2 — map Twilio message_status events onto sms_messages.status."""
    if not e.message_sid or not e.message_status:
        return
    await sms_svc.update_status_from_callback(
        message_sid=e.message_sid,
        new_status=e.message_status,
        error_code=e.error_code,
        callback_payload=e.raw_payload,
    )


async def _handle_inbound_message(e: TwilioEvent) -> None:
    """Drift fix §7.5 — record inbound SMS; STOP keyword → sms_suppressions."""
    if not e.message_sid:
        return

    await sms_svc.record_inbound_sms(
        brand_id=UUID(e.brand_id),
        message_sid=e.message_sid,
        account_sid=e.account_sid,
        from_number=e.from_number or "",
        to_number=e.to_number or "",
        body=e.message_body,
        payload=e.raw_payload,
    )

    if sms_svc.is_stop_keyword(e.message_body):
        await sms_svc.add_suppression(
            UUID(e.brand_id),
            e.from_number or "",
            reason="stop_keyword",
            notes=f"received from message_sid={e.message_sid}",
        )
        logger.info(
            "sms_stop_keyword_recorded",
            extra={
                "brand_id": e.brand_id,
                "from": e.from_number,
                "message_sid": e.message_sid,
            },
        )

    # §7.5 — attempt to link this inbound SMS to an open callback row
    # (last 48h). Phase 1 records the link only; LLM reschedule parsing
    # is deferred to v2 (§10 #3).
    linked_callback_id = await sms_svc.link_inbound_sms_to_callback(
        brand_id=UUID(e.brand_id),
        from_number=e.from_number or "",
        message_sid=e.message_sid,
    )
    if linked_callback_id is not None:
        logger.info(
            "sms_inbound_linked_to_callback",
            extra={
                "brand_id": e.brand_id,
                "from": e.from_number,
                "message_sid": e.message_sid,
                "callback_id": str(linked_callback_id),
            },
        )


_HANDLERS = {
    "call_status": _handle_call_status,
    "amd_result": _handle_amd_result,
    "recording_status": _handle_recording_status,
    "dial_action": _handle_dial_action,
    "message_status": _handle_message_status,
    "inbound_message": _handle_inbound_message,
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/{brand_id}")
async def ingest_twilio_webhook(brand_id: UUID, request: Request) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    sig_mode = settings.TWILIO_WEBHOOK_SIGNATURE_MODE

    if sig_mode != "disabled":
        try:
            creds = await brands_svc.get_twilio_creds(brand_id)
        except brands_svc.BrandCredsKeyMissing:
            creds = None
        if creds is None:
            if sig_mode == "enforce":
                raise HTTPException(status_code=403, detail="brand_creds_unavailable")
        else:
            signature = request.headers.get("X-Twilio-Signature", "")
            url = _reconstruct_url(request)
            valid = validate_twilio_signature(
                auth_token=creds.auth_token,
                url=url,
                params=params,
                signature=signature,
            )
            if not valid:
                logger.warning(
                    "twilio_webhook_signature_failed",
                    extra={"brand_id": str(brand_id), "mode": sig_mode},
                )
                if sig_mode == "enforce":
                    raise HTTPException(status_code=403, detail="signature_invalid")

    event = _parse(str(brand_id), params)
    if event.event_type == "unknown":
        logger.info(
            "twilio_webhook_unknown_event",
            extra={"brand_id": str(brand_id), "params_keys": list(params.keys())},
        )
        return Response(status_code=200)

    event_key = _event_key(event)
    is_new = await _persist_event_or_skip_duplicate(
        event_key=event_key,
        event_type=event.event_type,
        payload=params,
        brand_id=str(brand_id),
    )
    if not is_new:
        return Response(status_code=200)

    handler = _HANDLERS.get(event.event_type)
    if handler is None:
        return Response(status_code=200)

    try:
        await handler(event)
    except Exception:
        logger.exception(
            "twilio_webhook_handler_failed",
            extra={"event_type": event.event_type, "brand_id": str(brand_id)},
        )

    return Response(status_code=200)
