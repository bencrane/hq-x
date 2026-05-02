"""IVR TwiML router — synchronous call-flow endpoints called by Twilio.

Every endpoint returns text/xml TwiML. No endpoint requires user auth — they
are secured via Twilio HMAC-SHA1 signature validation against the brand's
decrypted Twilio auth_token.

Brand-scoped via path. URL paths are
``/api/voice/ivr/{brand_id}/{entry|step|gather|lookup|dial-action|record-action}/...``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio.twiml import (
    build_data_lookup_hold_response,
    build_dynamic_say_response,
    build_error_response,
    build_gather_dtmf_response,
    build_gather_speech_response,
    build_hangup_response,
    build_record_response,
    build_say_response,
    build_transfer_response,
)
from app.providers.twilio.webhooks import validate_twilio_signature
from app.services import brands as brands_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice/ivr", tags=["ivr"])


# ---------------------------------------------------------------------------
# Lookup type registry — extend by adding new handler functions
# ---------------------------------------------------------------------------


def _lookup_stub(input_value: str, config: dict | None) -> dict:
    """Stub lookup that always returns found. For testing and development."""
    return {"found": True, "data": {}}


_LOOKUP_HANDLERS: dict[str, Any] = {
    "stub": _lookup_stub,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _twiml_response(content: str) -> Response:
    return Response(content=content, media_type="text/xml")


def _reconstruct_url(request: Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", ""))
    path = request.url.path
    query = str(request.url.query) if request.url.query else ""
    url = f"{proto}://{host}{path}"
    if query:
        url += f"?{query}"
    return url


def _build_base_url(request: Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", ""))
    return f"{proto}://{host}"


def _build_step_url(request: Request, brand_id: UUID, session_id: str, step_key: str) -> str:
    return f"{_build_base_url(request)}/api/voice/ivr/{brand_id}/step/{session_id}/{step_key}"


def _build_gather_url(request: Request, brand_id: UUID, session_id: str, step_key: str) -> str:
    return f"{_build_base_url(request)}/api/voice/ivr/{brand_id}/gather/{session_id}/{step_key}"


def _build_lookup_url(request: Request, brand_id: UUID, session_id: str, step_key: str) -> str:
    return f"{_build_base_url(request)}/api/voice/ivr/{brand_id}/lookup/{session_id}/{step_key}"


def _build_dial_action_url(request: Request, brand_id: UUID, session_id: str, step_key: str) -> str:
    return f"{_build_base_url(request)}/api/voice/ivr/{brand_id}/dial-action/{session_id}/{step_key}"


def _build_record_action_url(request: Request, brand_id: UUID, session_id: str, step_key: str) -> str:
    return f"{_build_base_url(request)}/api/voice/ivr/{brand_id}/record-action/{session_id}/{step_key}"


async def _resolve_brand_auth_token(brand_id: UUID) -> str:
    try:
        creds = await brands_svc.get_twilio_creds(brand_id)
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    if creds is None:
        raise HTTPException(status_code=404, detail={"error": "brand_creds_not_found"})
    return creds.auth_token


async def _validate_twilio_request(
    request: Request,
    brand_id: UUID,
    auth_token: str,
) -> dict[str, str]:
    form_data = await request.form()
    params: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    webhook_url = _reconstruct_url(request)
    signature_valid = validate_twilio_signature(auth_token, webhook_url, params, twilio_signature)

    if not signature_valid:
        mode = settings.TWILIO_WEBHOOK_SIGNATURE_MODE
        logger.warning(
            "ivr_signature_failed brand_id=%s mode=%s",
            brand_id, mode,
        )
        if mode == "enforce":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Twilio signature",
            )
    return params


async def _resolve_flow_for_number(
    brand_id: UUID,
    called_number: str,
) -> tuple[dict, dict, list[dict]]:
    """Look up phone config → flow → steps for a called number."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, phone_number, phone_number_sid, flow_id, is_active
                FROM ivr_phone_configs
                WHERE brand_id = %s AND phone_number = %s
                  AND is_active = TRUE AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(brand_id), called_number),
            )
            pc_row = await cur.fetchone()
            if pc_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No IVR config for this number",
                )
            pc_cols = ["id", "brand_id", "phone_number", "phone_number_sid", "flow_id", "is_active"]
            phone_config = dict(zip(pc_cols, pc_row, strict=True))
            flow_id = phone_config["flow_id"]

            await cur.execute(
                """
                SELECT id, brand_id, name, description, is_active,
                       default_voice, default_language,
                       lookup_type, lookup_config,
                       default_transfer_number, transfer_timeout_seconds,
                       recording_enabled, recording_consent_required
                FROM ivr_flows
                WHERE id = %s AND brand_id = %s
                  AND is_active = TRUE AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(flow_id), str(brand_id)),
            )
            flow_row = await cur.fetchone()
            if flow_row is None:
                raise HTTPException(status_code=404, detail="IVR flow not found")
            flow_cols = [
                "id", "brand_id", "name", "description", "is_active",
                "default_voice", "default_language",
                "lookup_type", "lookup_config",
                "default_transfer_number", "transfer_timeout_seconds",
                "recording_enabled", "recording_consent_required",
            ]
            flow = dict(zip(flow_cols, flow_row, strict=True))

            await cur.execute(
                """
                SELECT id, flow_id, brand_id, step_key, step_type, position,
                       say_text, say_voice, say_language, audio_url,
                       gather_input, gather_num_digits, gather_timeout_seconds,
                       gather_finish_on_key, gather_max_retries,
                       gather_invalid_message, gather_validation_regex,
                       next_step_key, branches,
                       transfer_number, transfer_caller_id, transfer_record,
                       record_max_length_seconds, record_play_beep,
                       lookup_input_key, lookup_store_key
                FROM ivr_flow_steps
                WHERE flow_id = %s AND brand_id = %s
                ORDER BY position
                """,
                (str(flow_id), str(brand_id)),
            )
            step_rows = await cur.fetchall()
    step_cols = [
        "id", "flow_id", "brand_id", "step_key", "step_type", "position",
        "say_text", "say_voice", "say_language", "audio_url",
        "gather_input", "gather_num_digits", "gather_timeout_seconds",
        "gather_finish_on_key", "gather_max_retries",
        "gather_invalid_message", "gather_validation_regex",
        "next_step_key", "branches",
        "transfer_number", "transfer_caller_id", "transfer_record",
        "record_max_length_seconds", "record_play_beep",
        "lookup_input_key", "lookup_store_key",
    ]
    steps = [dict(zip(step_cols, r, strict=True)) for r in step_rows]
    return phone_config, flow, steps


async def _get_or_create_ivr_session(
    brand_id: UUID,
    call_sid: str,
    flow: dict,
    caller_number: str,
    called_number: str,
) -> dict:
    """Get existing IVR session or create a new one. Links to call_logs.id if present."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, call_log_id, flow_id, call_sid,
                       caller_number, called_number, current_step_key,
                       status, session_data, retry_counts,
                       transfer_result, disposition,
                       started_at, ended_at
                FROM ivr_sessions
                WHERE call_sid = %s AND brand_id = %s
                LIMIT 1
                """,
                (call_sid, str(brand_id)),
            )
            existing = await cur.fetchone()
            cols = [
                "id", "brand_id", "call_log_id", "flow_id", "call_sid",
                "caller_number", "called_number", "current_step_key",
                "status", "session_data", "retry_counts",
                "transfer_result", "disposition",
                "started_at", "ended_at",
            ]
            if existing is not None:
                return dict(zip(cols, existing, strict=True))

            # Try to link to a call_logs row for this call_sid
            await cur.execute(
                "SELECT id FROM call_logs WHERE twilio_call_sid = %s LIMIT 1",
                (call_sid,),
            )
            cl_row = await cur.fetchone()
            call_log_id = cl_row[0] if cl_row else None

            await cur.execute(
                """
                INSERT INTO ivr_sessions (
                    brand_id, call_log_id, flow_id, call_sid,
                    caller_number, called_number,
                    status, session_data, retry_counts
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'active', '{}'::jsonb, '{}'::jsonb)
                RETURNING id, brand_id, call_log_id, flow_id, call_sid,
                          caller_number, called_number, current_step_key,
                          status, session_data, retry_counts,
                          transfer_result, disposition,
                          started_at, ended_at
                """,
                (
                    str(brand_id), call_log_id,
                    str(flow["id"]), call_sid,
                    caller_number, called_number,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return dict(zip(cols, row, strict=True))


async def _get_session_and_flow(
    brand_id: UUID, session_id: str
) -> tuple[dict, dict, list[dict]]:
    """Load IVR session and its associated flow + steps."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, call_log_id, flow_id, call_sid,
                       caller_number, called_number, current_step_key,
                       status, session_data, retry_counts,
                       transfer_result, disposition,
                       started_at, ended_at
                FROM ivr_sessions
                WHERE id = %s AND brand_id = %s
                LIMIT 1
                """,
                (session_id, str(brand_id)),
            )
            sess_row = await cur.fetchone()
            if sess_row is None:
                raise HTTPException(status_code=404, detail="IVR session not found")
            sess_cols = [
                "id", "brand_id", "call_log_id", "flow_id", "call_sid",
                "caller_number", "called_number", "current_step_key",
                "status", "session_data", "retry_counts",
                "transfer_result", "disposition",
                "started_at", "ended_at",
            ]
            session = dict(zip(sess_cols, sess_row, strict=True))
            flow_id = session["flow_id"]

            await cur.execute(
                """
                SELECT id, brand_id, name, description, is_active,
                       default_voice, default_language,
                       lookup_type, lookup_config,
                       default_transfer_number, transfer_timeout_seconds,
                       recording_enabled, recording_consent_required
                FROM ivr_flows
                WHERE id = %s AND brand_id = %s
                LIMIT 1
                """,
                (str(flow_id), str(brand_id)),
            )
            flow_row = await cur.fetchone()
            flow_cols = [
                "id", "brand_id", "name", "description", "is_active",
                "default_voice", "default_language",
                "lookup_type", "lookup_config",
                "default_transfer_number", "transfer_timeout_seconds",
                "recording_enabled", "recording_consent_required",
            ]
            flow = dict(zip(flow_cols, flow_row, strict=True)) if flow_row else {}

            await cur.execute(
                """
                SELECT id, flow_id, brand_id, step_key, step_type, position,
                       say_text, say_voice, say_language, audio_url,
                       gather_input, gather_num_digits, gather_timeout_seconds,
                       gather_finish_on_key, gather_max_retries,
                       gather_invalid_message, gather_validation_regex,
                       next_step_key, branches,
                       transfer_number, transfer_caller_id, transfer_record,
                       record_max_length_seconds, record_play_beep,
                       lookup_input_key, lookup_store_key
                FROM ivr_flow_steps
                WHERE flow_id = %s AND brand_id = %s
                ORDER BY position
                """,
                (str(flow_id), str(brand_id)),
            )
            step_rows = await cur.fetchall()
    step_cols = [
        "id", "flow_id", "brand_id", "step_key", "step_type", "position",
        "say_text", "say_voice", "say_language", "audio_url",
        "gather_input", "gather_num_digits", "gather_timeout_seconds",
        "gather_finish_on_key", "gather_max_retries",
        "gather_invalid_message", "gather_validation_regex",
        "next_step_key", "branches",
        "transfer_number", "transfer_caller_id", "transfer_record",
        "record_max_length_seconds", "record_play_beep",
        "lookup_input_key", "lookup_store_key",
    ]
    steps = [dict(zip(step_cols, r, strict=True)) for r in step_rows]
    return session, flow, steps


def _get_step(steps: list[dict], step_key: str) -> dict | None:
    for step in steps:
        if step["step_key"] == step_key:
            return step
    return None


async def _update_session(session_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    set_parts: list[str] = []
    values: list[Any] = []
    json_columns = {"session_data", "retry_counts"}
    import json as _json
    for key, value in updates.items():
        if key in json_columns:
            set_parts.append(f"{key} = %s::jsonb")
            values.append(_json.dumps(value))
        else:
            set_parts.append(f"{key} = %s")
            values.append(value)
    set_parts.append("updated_at = NOW()")
    values.append(session_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE ivr_sessions SET {', '.join(set_parts)} WHERE id = %s",
                values,
            )
        await conn.commit()


def _resolve_branch(branches: list[dict] | None, condition: str) -> str | None:
    if not branches:
        return None
    for branch in branches:
        if branch.get("condition") == condition:
            return branch.get("next_step_key")
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{brand_id}/entry")
async def ivr_entry(brand_id: UUID, request: Request) -> Response:
    """Entry point — Twilio POSTs here when a call arrives."""
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        params = await _validate_twilio_request(request, brand_id, auth_token)

        call_sid = params.get("CallSid", "")
        caller_number = params.get("From", "")
        called_number = params.get("To", "")

        logger.info(
            "ivr_call_received brand_id=%s call_sid=%s from=%s to=%s",
            brand_id, call_sid, caller_number, called_number,
        )

        _phone_config, flow, steps = await _resolve_flow_for_number(brand_id, called_number)
        session = await _get_or_create_ivr_session(
            brand_id, call_sid, flow, caller_number, called_number,
        )

        if not steps:
            return _twiml_response(build_error_response())

        first_step = steps[0]
        redirect_url = _build_step_url(request, brand_id, str(session["id"]), first_step["step_key"])

        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()
        resp.redirect(redirect_url, method="POST")
        return _twiml_response(str(resp))

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_entry_error brand_id=%s detail=%s", brand_id, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_entry_error brand_id=%s err=%s", brand_id, exc)
        return _twiml_response(build_error_response())


@router.post("/{brand_id}/step/{session_id}/{step_key}")
async def ivr_step(
    brand_id: UUID, session_id: str, step_key: str, request: Request
) -> Response:
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        await _validate_twilio_request(request, brand_id, auth_token)

        session, flow, steps = await _get_session_and_flow(brand_id, session_id)
        step = _get_step(steps, step_key)
        if not step:
            logger.error("ivr_step_not_found brand=%s session=%s key=%s", brand_id, session_id, step_key)
            return _twiml_response(build_error_response())

        await _update_session(session_id, {"current_step_key": step_key})

        voice = step.get("say_voice") or flow.get("default_voice", "Polly.Joanna-Generative")
        language = step.get("say_language") or flow.get("default_language", "en-US")
        step_type = step["step_type"]
        say_text = step.get("say_text") or ""
        audio_url = step.get("audio_url")

        if step_type == "greeting":
            redirect_url = None
            if step.get("next_step_key"):
                redirect_url = _build_step_url(request, brand_id, session_id, step["next_step_key"])
            twiml = build_say_response(say_text, voice, language, redirect_url=redirect_url, audio_url=audio_url)

        elif step_type == "gather_dtmf":
            action_url = _build_gather_url(request, brand_id, session_id, step_key)
            fallback_url = None
            if step.get("next_step_key"):
                fallback_url = _build_step_url(request, brand_id, session_id, step["next_step_key"])
            twiml = build_gather_dtmf_response(
                prompt_text=say_text,
                action_url=action_url,
                voice=voice,
                language=language,
                num_digits=step.get("gather_num_digits"),
                timeout=step.get("gather_timeout_seconds") or 5,
                finish_on_key=step.get("gather_finish_on_key") or "#",
                fallback_url=fallback_url,
                audio_url=audio_url,
            )

        elif step_type == "gather_speech":
            action_url = _build_gather_url(request, brand_id, session_id, step_key)
            fallback_url = None
            if step.get("next_step_key"):
                fallback_url = _build_step_url(request, brand_id, session_id, step["next_step_key"])
            twiml = build_gather_speech_response(
                prompt_text=say_text,
                action_url=action_url,
                voice=voice,
                language=language,
                input_mode=step.get("gather_input") or "speech",
                timeout=step.get("gather_timeout_seconds") or 5,
                fallback_url=fallback_url,
                audio_url=audio_url,
            )

        elif step_type == "data_lookup":
            lookup_url = _build_lookup_url(request, brand_id, session_id, step_key)
            twiml = build_data_lookup_hold_response(say_text, lookup_url, voice, language, audio_url=audio_url)

        elif step_type == "say_dynamic":
            session_data = session.get("session_data") or {}
            redirect_url = None
            if step.get("next_step_key"):
                redirect_url = _build_step_url(request, brand_id, session_id, step["next_step_key"])
            twiml = build_dynamic_say_response(say_text, session_data, voice, language, redirect_url=redirect_url, audio_url=audio_url)

        elif step_type == "transfer":
            transfer_number = step.get("transfer_number") or flow.get("default_transfer_number")
            if not transfer_number:
                logger.error("ivr_transfer_no_number brand=%s session=%s", brand_id, session_id)
                return _twiml_response(build_error_response())
            action_url = _build_dial_action_url(request, brand_id, session_id, step_key)
            twiml = build_transfer_response(
                number=transfer_number,
                action_url=action_url,
                caller_id=step.get("transfer_caller_id"),
                timeout=flow.get("transfer_timeout_seconds") or 30,
                record=step.get("transfer_record") or "do-not-record",
            )

        elif step_type == "record":
            action_url = _build_record_action_url(request, brand_id, session_id, step_key)
            twiml = build_record_response(
                prompt_text=say_text,
                action_url=action_url,
                voice=voice,
                language=language,
                max_length=step.get("record_max_length_seconds") or 120,
                play_beep=step.get("record_play_beep", True),
                audio_url=audio_url,
            )

        elif step_type == "hangup":
            await _update_session(session_id, {"status": "completed", "ended_at": _now_iso()})
            twiml = build_hangup_response(goodbye_text=say_text or None, voice=voice, language=language, audio_url=audio_url)

        else:
            logger.error("ivr_unknown_step_type brand=%s type=%s", brand_id, step_type)
            return _twiml_response(build_error_response())

        return _twiml_response(twiml)

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_step_error brand=%s session=%s key=%s detail=%s", brand_id, session_id, step_key, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_step_error brand=%s session=%s key=%s err=%s", brand_id, session_id, step_key, exc)
        return _twiml_response(build_error_response())


@router.post("/{brand_id}/gather/{session_id}/{step_key}")
async def ivr_gather(
    brand_id: UUID, session_id: str, step_key: str, request: Request
) -> Response:
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        params = await _validate_twilio_request(request, brand_id, auth_token)

        session, flow, steps = await _get_session_and_flow(brand_id, session_id)
        step = _get_step(steps, step_key)
        if not step:
            return _twiml_response(build_error_response())

        input_value = params.get("Digits") or params.get("SpeechResult") or ""

        validation_regex = step.get("gather_validation_regex")
        if validation_regex and not re.match(validation_regex, input_value):
            retry_counts = session.get("retry_counts") or {}
            current_retries = retry_counts.get(step_key, 0)
            max_retries = step.get("gather_max_retries") or 2

            if current_retries < max_retries:
                retry_counts[step_key] = current_retries + 1
                await _update_session(session_id, {"retry_counts": retry_counts})

                voice = step.get("say_voice") or flow.get("default_voice", "Polly.Joanna-Generative")
                language = step.get("say_language") or flow.get("default_language", "en-US")
                invalid_msg = step.get("gather_invalid_message") or "Invalid input. Please try again."
                redirect_url = _build_step_url(request, brand_id, session_id, step_key)
                twiml = build_say_response(invalid_msg, voice, language, redirect_url=redirect_url)
                return _twiml_response(twiml)
            else:
                branches = step.get("branches") or []
                fallback_key = _resolve_branch(branches, "max_retries")
                if not fallback_key:
                    fallback_key = step.get("next_step_key")
                if fallback_key:
                    redirect_url = _build_step_url(request, brand_id, session_id, fallback_key)
                    from twilio.twiml.voice_response import VoiceResponse
                    resp = VoiceResponse()
                    resp.redirect(redirect_url, method="POST")
                    return _twiml_response(str(resp))
                return _twiml_response(build_error_response())

        # Valid input — store
        session_data = session.get("session_data") or {}
        session_data[step_key] = input_value
        await _update_session(session_id, {"session_data": session_data})

        branches = step.get("branches") or []
        next_key = None
        for branch in branches:
            condition = branch.get("condition", "")
            if condition and condition in session_data:
                next_key = branch.get("next_step_key")
                break
        if not next_key:
            next_key = step.get("next_step_key")

        if next_key:
            redirect_url = _build_step_url(request, brand_id, session_id, next_key)
            from twilio.twiml.voice_response import VoiceResponse
            resp = VoiceResponse()
            resp.redirect(redirect_url, method="POST")
            return _twiml_response(str(resp))

        return _twiml_response(build_hangup_response())

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_gather_error brand=%s session=%s detail=%s", brand_id, session_id, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_gather_error brand=%s session=%s err=%s", brand_id, session_id, exc)
        return _twiml_response(build_error_response())


@router.post("/{brand_id}/lookup/{session_id}/{step_key}")
async def ivr_lookup(
    brand_id: UUID, session_id: str, step_key: str, request: Request
) -> Response:
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        await _validate_twilio_request(request, brand_id, auth_token)

        session, flow, steps = await _get_session_and_flow(brand_id, session_id)
        step = _get_step(steps, step_key)
        if not step:
            return _twiml_response(build_error_response())

        session_data = session.get("session_data") or {}
        lookup_input_key = step.get("lookup_input_key") or step_key
        input_value = session_data.get(lookup_input_key, "")

        lookup_type = flow.get("lookup_type") or "stub"
        lookup_config = flow.get("lookup_config")
        store_key = step.get("lookup_store_key") or "lookup_result"

        handler = _LOOKUP_HANDLERS.get(lookup_type)
        if handler:
            try:
                result = handler(input_value, lookup_config)
            except Exception as exc:  # noqa: BLE001
                logger.error("ivr_lookup_failed brand=%s type=%s err=%s", brand_id, lookup_type, exc)
                result = {"found": False, "error": str(exc)}
        else:
            logger.warning("ivr_lookup_type_unknown brand=%s type=%s", brand_id, lookup_type)
            result = {"found": False, "error": f"Unknown lookup type: {lookup_type}"}

        session_data[store_key] = result
        await _update_session(session_id, {"session_data": session_data})

        branches = step.get("branches") or []
        found = result.get("found", False)
        if found:
            next_key = _resolve_branch(branches, "lookup_found")
        else:
            next_key = _resolve_branch(branches, "lookup_not_found")
        if not next_key:
            next_key = step.get("next_step_key")

        if next_key:
            redirect_url = _build_step_url(request, brand_id, session_id, next_key)
            from twilio.twiml.voice_response import VoiceResponse
            resp = VoiceResponse()
            resp.redirect(redirect_url, method="POST")
            return _twiml_response(str(resp))

        return _twiml_response(build_hangup_response())

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_lookup_error brand=%s detail=%s", brand_id, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_lookup_error brand=%s err=%s", brand_id, exc)
        return _twiml_response(build_error_response())


@router.post("/{brand_id}/dial-action/{session_id}/{step_key}")
async def ivr_dial_action(
    brand_id: UUID, session_id: str, step_key: str, request: Request
) -> Response:
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        params = await _validate_twilio_request(request, brand_id, auth_token)

        session, flow, steps = await _get_session_and_flow(brand_id, session_id)
        step = _get_step(steps, step_key)
        if not step:
            return _twiml_response(build_error_response())

        dial_call_status = params.get("DialCallStatus", "")
        dial_call_sid = params.get("DialCallSid", "")
        dial_call_duration = params.get("DialCallDuration")
        dial_bridged = params.get("DialBridged", "").lower() == "true"

        session_data = session.get("session_data") or {}
        session_data["dial_result"] = {
            "status": dial_call_status,
            "call_sid": dial_call_sid,
            "duration": dial_call_duration,
            "bridged": dial_bridged,
        }

        await _update_session(session_id, {
            "transfer_result": dial_call_status,
            "session_data": session_data,
        })

        branches = step.get("branches") or []

        if dial_call_status == "completed":
            await _update_session(session_id, {"status": "transferred"})
            next_key = _resolve_branch(branches, "transfer_completed")
            if not next_key:
                next_key = step.get("next_step_key")
        else:
            next_key = _resolve_branch(branches, "transfer_failed")

        if next_key:
            redirect_url = _build_step_url(request, brand_id, session_id, next_key)
            from twilio.twiml.voice_response import VoiceResponse
            resp = VoiceResponse()
            resp.redirect(redirect_url, method="POST")
            return _twiml_response(str(resp))

        voice = flow.get("default_voice", "Polly.Joanna-Generative")
        language = flow.get("default_language", "en-US")
        if dial_call_status == "completed":
            twiml = build_hangup_response(goodbye_text="Thank you for calling. Goodbye.", voice=voice, language=language)
        else:
            twiml = build_hangup_response(
                goodbye_text="We're sorry, we were unable to connect your call. Please try again later.",
                voice=voice, language=language,
            )

        return _twiml_response(twiml)

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_dial_action_error brand=%s detail=%s", brand_id, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_dial_action_error brand=%s err=%s", brand_id, exc)
        return _twiml_response(build_error_response())


@router.post("/{brand_id}/record-action/{session_id}/{step_key}")
async def ivr_record_action(
    brand_id: UUID, session_id: str, step_key: str, request: Request
) -> Response:
    try:
        auth_token = await _resolve_brand_auth_token(brand_id)
        params = await _validate_twilio_request(request, brand_id, auth_token)

        session, _flow, steps = await _get_session_and_flow(brand_id, session_id)
        step = _get_step(steps, step_key)
        if not step:
            return _twiml_response(build_error_response())

        session_data = session.get("session_data") or {}
        session_data["recording"] = {
            "url": params.get("RecordingUrl", ""),
            "duration": params.get("RecordingDuration", ""),
            "digits": params.get("Digits", ""),
        }
        await _update_session(session_id, {"session_data": session_data})

        next_key = step.get("next_step_key")
        if next_key:
            redirect_url = _build_step_url(request, brand_id, session_id, next_key)
            from twilio.twiml.voice_response import VoiceResponse
            resp = VoiceResponse()
            resp.redirect(redirect_url, method="POST")
            return _twiml_response(str(resp))

        return _twiml_response(build_hangup_response())

    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        logger.warning("ivr_record_action_error brand=%s detail=%s", brand_id, exc.detail)
        return _twiml_response(build_error_response())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ivr_record_action_error brand=%s err=%s", brand_id, exc)
        return _twiml_response(build_error_response())
