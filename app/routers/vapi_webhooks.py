"""Vapi webhook ingress router.

Single POST endpoint that receives all Vapi webhook events, verifies the
X-Vapi-Secret header, and routes to the appropriate handler.

Vapi sends two types of events:
- Response-required (assistant-request, tool-calls, transfer-destination-request):
  Must return JSON response.
- Informational (status-update, end-of-call-report, transcript):
  Persist to webhook_events idempotently, dispatch handler, return 200.

Brand resolution cascade (per directive §6):
  1. voice_phone_numbers.vapi_phone_number_id → brand_id
  2. voice_phone_numbers.phone_number       → brand_id
  3. call_logs.vapi_call_id                 → brand_id
  Else: still return 200 (don't trigger Vapi retries) but log + skip handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import get_db_connection
from app.services import voice_inbound_routing
from app.services.call_analytics import record_call_completion
from app.services.voice_ai_tools import (
    dispatch_tool_calls,
    resolve_transfer_destination,
)

router = APIRouter(prefix="/api/v1/vapi", tags=["webhooks-vapi"])
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _stable_payload_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------


def _extract_phone_number(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    call = message.get("call", {})
    phone_obj = call.get("phoneNumber") or {}
    if isinstance(phone_obj, dict) and phone_obj.get("number"):
        return phone_obj["number"]
    return call.get("phoneNumber") if isinstance(call.get("phoneNumber"), str) else None


def _extract_vapi_phone_number_id(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    call = message.get("call", {})
    phone_obj = call.get("phoneNumber") or {}
    if isinstance(phone_obj, dict) and phone_obj.get("id"):
        return phone_obj["id"]
    pid = call.get("phoneNumberId")
    return pid if isinstance(pid, str) else None


def _extract_vapi_call_id(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    call = message.get("call", {})
    return call.get("id")


def _extract_call_direction(payload: dict[str, Any]) -> str:
    message = payload.get("message", {})
    call = message.get("call", {})
    if call.get("type") == "inboundPhoneCall":
        return "inbound"
    return "outbound"


def _extract_customer_number(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    call = message.get("call", {})
    customer = call.get("customer") or {}
    if isinstance(customer, dict) and customer.get("number"):
        return customer["number"]
    return None


def _extract_transcript_text(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    candidates = [
        message.get("transcript"), message.get("text"), message.get("content"),
        payload.get("transcript"), payload.get("text"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, dict):
            nested = c.get("text") or c.get("transcript")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def _extract_transcript_timestamp(payload: dict[str, Any]) -> str | None:
    message = payload.get("message", {})
    for key in ("timestamp", "time", "createdAt", "updatedAt"):
        v = message.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_transcript_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message", {})
    metadata: dict[str, Any] = {}
    for key in (
        "role", "speaker", "channel", "chunkIndex", "index",
        "isFinal", "final", "start", "end", "startOffsetMs",
        "endOffsetMs", "secondsFromStart", "utteranceId", "timestamp",
    ):
        value = message.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "1", "yes"}:
            return True
        if s in {"false", "0", "no"}:
            return False
    return None


# ---------------------------------------------------------------------------
# Brand resolution cascade (directive §6)
# ---------------------------------------------------------------------------


async def _resolve_brand_id(
    *,
    vapi_phone_number_id: str | None,
    phone_number: str | None,
    vapi_call_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Returns (brand_id, ctx) where ctx may carry voice_assistant_id /
    voice_phone_number_id / channel_campaign_id / partner_id from the lookup."""

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if vapi_phone_number_id:
                await cur.execute(
                    """
                    SELECT brand_id, voice_assistant_id, id, partner_id, channel_campaign_id
                    FROM voice_phone_numbers
                    WHERE vapi_phone_number_id = %s AND deleted_at IS NULL
                    LIMIT 2
                    """,
                    (vapi_phone_number_id,),
                )
                rows = await cur.fetchall()
                if len(rows) == 1:
                    r = rows[0]
                    return str(r[0]), {
                        "voice_assistant_id": r[1], "voice_phone_number_id": r[2],
                        "partner_id": r[3], "channel_campaign_id": r[4],
                    }

            if phone_number:
                await cur.execute(
                    """
                    SELECT brand_id, voice_assistant_id, id, partner_id, channel_campaign_id
                    FROM voice_phone_numbers
                    WHERE phone_number = %s AND deleted_at IS NULL
                    LIMIT 2
                    """,
                    (phone_number,),
                )
                rows = await cur.fetchall()
                if len(rows) == 1:
                    r = rows[0]
                    return str(r[0]), {
                        "voice_assistant_id": r[1], "voice_phone_number_id": r[2],
                        "partner_id": r[3], "channel_campaign_id": r[4],
                    }

            if vapi_call_id:
                await cur.execute(
                    """
                    SELECT brand_id, voice_assistant_id, voice_phone_number_id,
                           partner_id, channel_campaign_id
                    FROM call_logs
                    WHERE vapi_call_id = %s
                    LIMIT 1
                    """,
                    (vapi_call_id,),
                )
                row = await cur.fetchone()
                if row is not None:
                    return str(row[0]), {
                        "voice_assistant_id": row[1], "voice_phone_number_id": row[2],
                        "partner_id": row[3], "channel_campaign_id": row[4],
                    }

    return None, {}


# ---------------------------------------------------------------------------
# Idempotent informational-event persistence
# ---------------------------------------------------------------------------


def _build_info_event_key(
    vapi_call_id: str | None, event_type: str, payload: dict[str, Any]
) -> str:
    call_token = vapi_call_id or "unknown"
    message = payload.get("message", {})
    call = message.get("call", {})

    if event_type == "status-update":
        status_value = message.get("status") or call.get("status") or "unknown"
        return f"vapi:{call_token}:{event_type}:{status_value}"

    if event_type == "end-of-call-report":
        ended_at = call.get("endedAt") or message.get("timestamp")
        if ended_at:
            return f"vapi:{call_token}:{event_type}:{ended_at}"

    return f"vapi:{call_token}:{event_type}:{_stable_payload_hash(message or payload)}"


async def _persist_event_or_skip_duplicate(
    *,
    event_key: str,
    event_type: str,
    payload: dict[str, Any],
    brand_id: str,
) -> bool:
    """Insert into webhook_events. Returns True if new, False if duplicate."""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO webhook_events (
                        provider_slug, event_key, event_type, status,
                        replay_count, payload, brand_id
                    )
                    VALUES (%s, %s, %s, 'processed', 0, %s::jsonb, %s)
                    """,
                    ("vapi", event_key, event_type, json.dumps(payload), brand_id),
                )
            await conn.commit()
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            return False
        raise


# ---------------------------------------------------------------------------
# Response handlers (must return synchronously to Vapi within 7.5s)
# ---------------------------------------------------------------------------


async def _handle_assistant_request(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> dict[str, Any]:
    if _extract_call_direction(payload) == "inbound":
        return await _handle_inbound_assistant_request(payload, brand_id)
    return await _handle_outbound_assistant_request(payload, brand_id, ctx)


async def _handle_inbound_assistant_request(
    payload: dict[str, Any], brand_id: str
) -> dict[str, Any]:
    to_phone = _extract_phone_number(payload)
    from_phone = _extract_customer_number(payload)
    if not to_phone:
        logger.warning("vapi_inbound_request_no_phone", extra={"brand_id": brand_id})
        return {"error": "No phone number found in request"}

    routing = await voice_inbound_routing.resolve_inbound_assistant(
        to_phone, from_phone or "", brand_id=UUID(brand_id),
    )
    if not routing:
        logger.warning(
            "vapi_inbound_request_unconfigured",
            extra={"brand_id": brand_id, "phone_number": to_phone},
        )
        return {"error": "This number is not configured for inbound calls"}

    routing_mode = routing.get("routing_mode", "static")
    assistant_config = routing.get("assistant_config", {})

    if routing_mode == "static" and "assistantId" in assistant_config:
        return {"assistantId": assistant_config["assistantId"]}

    if "assistant" in assistant_config:
        config = assistant_config["assistant"]
    elif "assistantId" in assistant_config:
        return {"assistantId": assistant_config["assistantId"]}
    else:
        config = dict(assistant_config)

    caller_context = routing.get("caller_context")
    if caller_context and config.get("model", {}).get("messages"):
        for msg in config["model"]["messages"]:
            if msg.get("role") == "system":
                msg["content"] = (msg.get("content") or "") + (
                    f"\n\nCaller context: Name={caller_context.get('name', 'Unknown')}, "
                    f"Company={caller_context.get('company', 'Unknown')}, "
                    f"Previous outcome={caller_context.get('previous_outcome', 'None')}"
                )
                break

    if routing.get("first_message_mode"):
        config["firstMessageMode"] = routing["first_message_mode"]

    overrides = routing.get("inbound_overrides")
    if isinstance(overrides, dict):
        config.update(overrides)

    return {"assistant": config}


async def _handle_outbound_assistant_request(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Outbound assistant config from voice_phone_numbers + voice_assistants."""
    phone_number = _extract_phone_number(payload)
    if not phone_number:
        logger.warning("vapi_assistant_request_no_phone", extra={"brand_id": brand_id})
        return {}

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT voice_assistant_id, id
                FROM voice_phone_numbers
                WHERE phone_number = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (phone_number, brand_id),
            )
            phone_row = await cur.fetchone()
            if phone_row is None:
                logger.warning(
                    "vapi_assistant_request_phone_not_found",
                    extra={"brand_id": brand_id, "phone_number": phone_number},
                )
                return {}

            assistant_id = phone_row[0]
            if assistant_id is None:
                return {}

            await cur.execute(
                """
                SELECT name, vapi_assistant_id, system_prompt, first_message,
                       first_message_mode, model_config, voice_config,
                       transcriber_config, tools_config, analysis_config,
                       max_duration_seconds, metadata
                FROM voice_assistants
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(assistant_id), brand_id),
            )
            assistant_row = await cur.fetchone()

    if assistant_row is None:
        return {}

    (
        name, vapi_assistant_id, system_prompt, first_message,
        first_message_mode, model_config, voice_config,
        transcriber_config, tools_config, analysis_config,
        max_duration_seconds, metadata,
    ) = assistant_row

    if vapi_assistant_id:
        return {"assistantId": vapi_assistant_id}

    config: dict[str, Any] = {"name": name}
    if system_prompt:
        model_cfg = dict(model_config or {})
        model_cfg["messages"] = [{"role": "system", "content": system_prompt}]
        config["model"] = model_cfg
    if first_message:
        config["firstMessage"] = first_message
    if first_message_mode:
        config["firstMessageMode"] = first_message_mode
    if voice_config:
        config["voice"] = voice_config
    if transcriber_config:
        config["transcriber"] = transcriber_config
    if tools_config:
        config["tools"] = tools_config
    if analysis_config:
        config["analysisPlan"] = analysis_config
    if max_duration_seconds:
        config["maxDurationSeconds"] = max_duration_seconds
    if metadata:
        config["metadata"] = metadata
    return {"assistant": config}


async def _handle_tool_calls(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> dict[str, Any]:
    message = payload.get("message", {})
    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if not tool_calls:
        return {"results": []}
    results = await dispatch_tool_calls(tool_calls, brand_id)
    return {"results": results}


async def _handle_transfer_destination_request(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> dict[str, Any]:
    vapi_call_id = _extract_vapi_call_id(payload)
    channel_campaign_id: str | None = ctx.get("channel_campaign_id")

    if not channel_campaign_id and vapi_call_id:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT channel_campaign_id
                    FROM call_logs
                    WHERE vapi_call_id = %s AND brand_id = %s
                    LIMIT 1
                    """,
                    (vapi_call_id, brand_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    channel_campaign_id = str(row[0]) if row[0] else None

    destination = await resolve_transfer_destination(
        brand_id=brand_id, channel_campaign_id=channel_campaign_id
    )
    if not destination:
        logger.warning(
            "vapi_transfer_no_destination",
            extra={"brand_id": brand_id, "vapi_call_id": vapi_call_id},
        )
        return {"error": "No matching transfer destination found"}

    return {
        "destination": {
            "type": "number",
            "number": destination["destination_phone"],
            "message": "Transferring you now...",
        }
    }


# ---------------------------------------------------------------------------
# Informational handlers (fire-and-forget after persistence)
# ---------------------------------------------------------------------------


async def _handle_status_update(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> None:
    message = payload.get("message", {})
    call = message.get("call", {})
    vapi_call_id = call.get("id")
    new_status = message.get("status") or call.get("status") or "queued"
    if not vapi_call_id:
        return

    phone_obj = call.get("phoneNumber") or {}
    from_number = phone_obj.get("number") if isinstance(phone_obj, dict) else None
    customer = call.get("customer") or {}
    customer_number = customer.get("number") if isinstance(customer, dict) else None

    started_at = _now() if new_status == "in-progress" else None
    ended_at = _now() if new_status == "ended" else None

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Postgres ON CONFLICT requires explicit UNIQUE — we have one on vapi_call_id.
            await cur.execute(
                """
                INSERT INTO call_logs (
                    vapi_call_id, brand_id, status, from_number, customer_number,
                    started_at, ended_at, direction, voice_assistant_id,
                    voice_phone_number_id, partner_id, channel_campaign_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vapi_call_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    from_number = COALESCE(EXCLUDED.from_number, call_logs.from_number),
                    customer_number = COALESCE(EXCLUDED.customer_number, call_logs.customer_number),
                    started_at = COALESCE(EXCLUDED.started_at, call_logs.started_at),
                    ended_at = COALESCE(EXCLUDED.ended_at, call_logs.ended_at),
                    updated_at = NOW()
                """,
                (
                    vapi_call_id, brand_id, new_status, from_number, customer_number,
                    started_at, ended_at, _extract_call_direction(payload),
                    str(ctx.get("voice_assistant_id")) if ctx.get("voice_assistant_id") else None,
                    str(ctx.get("voice_phone_number_id")) if ctx.get("voice_phone_number_id") else None,
                    str(ctx.get("partner_id")) if ctx.get("partner_id") else None,
                    str(ctx.get("channel_campaign_id"))
                    if ctx.get("channel_campaign_id") else None,
                ),
            )
        await conn.commit()


async def _handle_end_of_call_report(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> None:
    message = payload.get("message", {})
    call = message.get("call", {})
    artifact = message.get("artifact", {})
    analysis = message.get("analysis", {})

    vapi_call_id = call.get("id")
    if not vapi_call_id:
        return

    cost_breakdown = call.get("costBreakdown") or {}
    cost_total: float | None = None
    if cost_breakdown:
        try:
            cost_total = sum(
                float(v) for v in cost_breakdown.values()
                if isinstance(v, (int, float))
                or (isinstance(v, str) and v.replace(".", "", 1).replace("-", "", 1).isdigit())
            )
        except (TypeError, ValueError):
            cost_total = None

    transcript_text = artifact.get("transcript")
    transcript_messages = artifact.get("messages")
    recording_url = artifact.get("recordingUrl")

    fields: dict[str, Any] = {
        "status": "ended",
        "ended_reason": call.get("endedReason"),
        "duration_seconds": call.get("duration"),
        "transcript": transcript_text,
        "transcript_messages": transcript_messages,
        "recording_url": recording_url,
        "structured_data": analysis.get("structuredData"),
        "analysis_summary": analysis.get("summary"),
        "success_evaluation": analysis.get("successEvaluation"),
        "cost_breakdown": cost_breakdown if cost_breakdown else None,
        "cost_total": cost_total,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return

    set_parts: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if isinstance(value, dict) or isinstance(value, list):
            set_parts.append(f"{key} = %s::jsonb")
            values.append(json.dumps(value))
        else:
            set_parts.append(f"{key} = %s")
            values.append(value)
    set_parts.append("updated_at = NOW()")
    values.extend([vapi_call_id, brand_id])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE call_logs
                SET {", ".join(set_parts)}
                WHERE vapi_call_id = %s AND brand_id = %s
                """,
                values,
            )
        await conn.commit()

    # ClickHouse dual-write (fire-and-forget).
    try:
        record_call_completion(
            brand_id=brand_id,
            call_data={
                "vapi_call_id": vapi_call_id,
                "direction": _extract_call_direction(payload),
                "outcome": fields.get("analysis_summary") or "",
                "duration_seconds": call.get("duration") or 0,
                "ended_reason": call.get("endedReason") or "",
                "success_evaluation": analysis.get("successEvaluation") or "",
                "channel_campaign_id": ctx.get("channel_campaign_id"),
                "partner_id": ctx.get("partner_id"),
            },
            cost_breakdown=cost_breakdown or {},
        )
    except Exception as exc:
        logger.warning(
            "clickhouse_dual_write_failed",
            extra={"brand_id": brand_id, "error": str(exc)},
        )


async def _handle_transcript(
    payload: dict[str, Any], brand_id: str, ctx: dict[str, Any]
) -> None:
    vapi_call_id = _extract_vapi_call_id(payload)
    event_key = _build_info_event_key(vapi_call_id, "transcript", payload)
    transcript_text = _extract_transcript_text(payload)
    transcript_timestamp = _extract_transcript_timestamp(payload)
    metadata = _extract_transcript_metadata(payload)
    message = payload.get("message", {})

    call_log_id = None
    if vapi_call_id:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id FROM call_logs
                    WHERE vapi_call_id = %s AND brand_id = %s
                    LIMIT 1
                    """,
                    (vapi_call_id, brand_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    call_log_id = str(row[0])

    is_final = _coerce_bool(
        message.get("isFinal") if message.get("isFinal") is not None else message.get("final")
    )
    chunk_index = (
        message.get("chunkIndex")
        if isinstance(message.get("chunkIndex"), int)
        else message.get("index")
    )

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO vapi_transcript_events (
                        brand_id, call_log_id, vapi_call_id, event_key,
                        event_timestamp, speaker, channel, is_final,
                        chunk_index, transcript_text, metadata, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (event_key) DO NOTHING
                    """,
                    (
                        brand_id,
                        call_log_id,
                        vapi_call_id,
                        event_key,
                        transcript_timestamp,
                        message.get("speaker") or message.get("role"),
                        message.get("channel"),
                        is_final,
                        chunk_index,
                        transcript_text,
                        json.dumps(metadata) if metadata else None,
                        json.dumps(payload),
                    ),
                )
            await conn.commit()
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            return
        raise


# ---------------------------------------------------------------------------
# Dispatch maps
# ---------------------------------------------------------------------------

_RESPONSE_HANDLERS = {
    "assistant-request": _handle_assistant_request,
    "tool-calls": _handle_tool_calls,
    "transfer-destination-request": _handle_transfer_destination_request,
}

_INFO_HANDLERS = {
    "status-update": _handle_status_update,
    "end-of-call-report": _handle_end_of_call_report,
    "transcript": _handle_transcript,
}


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@router.post("/webhook")
async def ingest_vapi_webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        ) from exc

    # Signature verification.
    secret_header = request.headers.get("X-Vapi-Secret", "")
    mode = settings.VAPI_WEBHOOK_SIGNATURE_MODE
    expected = settings.VAPI_WEBHOOK_SECRET.get_secret_value() if settings.VAPI_WEBHOOK_SECRET else ""

    if mode != "disabled":
        secret_valid = (
            hmac.compare_digest(secret_header, expected)
            if expected else False
        )
        if not secret_valid:
            logger.warning(
                "vapi_webhook_signature_failed",
                extra={"mode": mode},
            )
            if mode == "strict":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid Vapi webhook secret",
                )

    message = payload.get("message", {})
    event_type = message.get("type", "unknown")
    vapi_call_id = _extract_vapi_call_id(payload)
    vapi_phone_number_id = _extract_vapi_phone_number_id(payload)
    phone_number = _extract_phone_number(payload)

    brand_id, ctx = await _resolve_brand_id(
        vapi_phone_number_id=vapi_phone_number_id,
        phone_number=phone_number,
        vapi_call_id=vapi_call_id,
    )

    if not brand_id:
        logger.warning(
            "vapi_webhook_brand_not_resolved",
            extra={
                "event_type": event_type,
                "vapi_call_id": vapi_call_id,
                "vapi_phone_number_id": vapi_phone_number_id,
                "phone_number": phone_number,
            },
        )
        # Still 200 so Vapi doesn't retry forever.
        return JSONResponse(content={}, status_code=200)

    # Response-required events.
    if event_type in _RESPONSE_HANDLERS:
        handler = _RESPONSE_HANDLERS[event_type]
        try:
            result = await handler(payload, brand_id, ctx)
            return JSONResponse(content=result, status_code=200)
        except Exception:
            logger.exception(
                "vapi_webhook_response_handler_failed",
                extra={"event_type": event_type, "brand_id": brand_id},
            )
            return JSONResponse(content={}, status_code=200)

    # Informational events — persist idempotently, then dispatch.
    if event_type in _INFO_HANDLERS:
        event_key = _build_info_event_key(vapi_call_id, event_type, payload)
        is_new = await _persist_event_or_skip_duplicate(
            event_key=event_key,
            event_type=event_type,
            payload=payload,
            brand_id=brand_id,
        )
        if not is_new:
            return JSONResponse(content={}, status_code=200)

        handler = _INFO_HANDLERS[event_type]
        try:
            await handler(payload, brand_id, ctx)
        except Exception:
            logger.exception(
                "vapi_webhook_info_handler_failed",
                extra={"event_type": event_type, "brand_id": brand_id},
            )
        return JSONResponse(content={}, status_code=200)

    logger.warning(
        "vapi_webhook_unknown_event",
        extra={"event_type": event_type, "brand_id": brand_id},
    )
    return JSONResponse(content={}, status_code=200)
