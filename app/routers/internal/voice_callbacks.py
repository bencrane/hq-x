"""Internal voice-callback endpoints driven by Trigger.dev tasks.

Two endpoints:

  * ``POST /internal/voice/callback/send-reminders``  (§7.6)
      Reads voice_callback_requests rows whose preferred_time is within the
      next 20 minutes and have not yet had a reminder sent. For each, sends
      a templated SMS via ``app.services.sms.send_sms`` (which goes through
      the suppression check) and stamps ``reminder_sent_at`` /
      ``reminder_sms_sid`` on the row.

  * ``POST /internal/voice/callback/run-due-callbacks``  (§7.7)
      Reads scheduled callback rows whose preferred_time has elapsed,
      claims them with ``status='processing'``, and fires an outbound Vapi
      call. If ``leave_voicemail_on_no_answer=true`` and a script is set,
      the call is configured via ``assistantOverrides`` so Vapi's TTS
      leaves the voicemail when AMD detects a machine.

Both endpoints accept the trigger shared secret (system caller) or an
operator JWT via ``require_flexible_auth``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.services import brands as brands_svc
from app.services.sms import SmsSuppressedError, send_sms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/callback", tags=["internal"])


_REMINDER_SUPPRESSED_SENTINEL = "__suppressed__"

_REMINDER_TEMPLATE = (
    "Reminder: your callback is scheduled for {time}. Reply STOP to opt out."
)


def _format_preferred_time(preferred_time: Any, tz_label: str | None) -> str:
    """Best-effort human-friendly preferred-time string for the reminder body."""
    try:
        # ``preferred_time`` is a TIMESTAMPTZ → datetime in psycopg.
        return preferred_time.strftime("%Y-%m-%d %H:%M %Z").strip() or str(preferred_time)
    except Exception:
        return str(preferred_time)


# ---------------------------------------------------------------------------
# §7.6 — send-reminders
# ---------------------------------------------------------------------------


@router.post("/send-reminders")
async def send_callback_reminders(
    _ctx: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Send reminder SMSes for callbacks whose preferred_time is within
    the next 20 minutes and haven't yet had a reminder logged."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, customer_number, preferred_time, timezone
                FROM voice_callback_requests
                WHERE status = 'scheduled'
                  AND deleted_at IS NULL
                  AND reminder_sent_at IS NULL
                  AND preferred_time BETWEEN NOW() AND NOW() + INTERVAL '20 minutes'
                ORDER BY preferred_time
                LIMIT 50
                """
            )
            rows = await cur.fetchall()

    processed = 0
    sent = 0
    suppressed = 0
    errors: list[dict[str, str]] = []

    for row in rows:
        callback_id, brand_id, customer_number, preferred_time, tz_label = row
        processed += 1

        if not customer_number:
            errors.append({"id": str(callback_id), "reason": "no_customer_number"})
            continue

        try:
            creds = await brands_svc.get_twilio_creds(brand_id)
        except brands_svc.BrandCredsKeyMissing:
            creds = None
        if creds is None:
            errors.append({"id": str(callback_id), "reason": "brand_creds_missing"})
            continue

        # Resolve sending number — prefer the messaging service if the brand
        # has one; fall back to the callback's voice phone number row.
        from_number, messaging_service_sid = await _resolve_sender(brand_id, callback_id)
        if from_number is None and messaging_service_sid is None:
            errors.append({"id": str(callback_id), "reason": "no_sender_configured"})
            continue

        body = _REMINDER_TEMPLATE.format(
            time=_format_preferred_time(preferred_time, tz_label)
        )

        try:
            result = await send_sms(
                brand_id=brand_id,
                account_sid=creds.account_sid,
                auth_token=creds.auth_token,
                to=customer_number,
                body=body,
                from_number=from_number,
                messaging_service_sid=messaging_service_sid,
            )
        except SmsSuppressedError:
            await _stamp_reminder_suppressed(callback_id)
            suppressed += 1
            continue
        except Exception as exc:  # pragma: no cover — provider faults
            logger.warning(
                "callback_reminder_send_failed",
                extra={"callback_id": str(callback_id), "error": str(exc)},
            )
            errors.append({"id": str(callback_id), "reason": str(exc)[:200]})
            continue

        await _stamp_reminder_sent(callback_id, result["message_sid"])
        sent += 1

    return {
        "processed": processed,
        "sent": sent,
        "suppressed": suppressed,
        "errors": errors,
    }


async def _resolve_sender(
    brand_id: UUID, callback_id: UUID
) -> tuple[str | None, str | None]:
    """Return ``(from_number, messaging_service_sid)`` for the reminder send."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT twilio_messaging_service_sid
                FROM business.brands
                WHERE id = %s AND deleted_at IS NULL
                """,
                (str(brand_id),),
            )
            row = await cur.fetchone()
            messaging_service_sid = row[0] if row else None

            if messaging_service_sid:
                return None, messaging_service_sid

            await cur.execute(
                """
                SELECT vpn.phone_number
                FROM voice_callback_requests vcb
                LEFT JOIN voice_phone_numbers vpn
                    ON vpn.id = vcb.voice_phone_number_id
                WHERE vcb.id = %s
                LIMIT 1
                """,
                (str(callback_id),),
            )
            row = await cur.fetchone()
            from_number = row[0] if row else None
    return from_number, None


async def _stamp_reminder_sent(callback_id: UUID, message_sid: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_callback_requests
                SET reminder_sent_at = NOW(),
                    reminder_sms_sid = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (message_sid, str(callback_id)),
            )
        await conn.commit()


async def _stamp_reminder_suppressed(callback_id: UUID) -> None:
    """Stamp the row so we don't retry the reminder forever."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_callback_requests
                SET reminder_sent_at = NOW(),
                    reminder_sms_sid = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (_REMINDER_SUPPRESSED_SENTINEL, str(callback_id)),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# §7.7 — run-due-callbacks
# ---------------------------------------------------------------------------


@router.post("/run-due-callbacks")
async def run_due_callbacks(
    _ctx: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Fire outbound Vapi calls for callbacks whose preferred_time has passed.

    Per §7.7: if ``leave_voicemail_on_no_answer=true`` and ``voicemail_script``
    is set, the Vapi call is configured via ``assistantOverrides`` so the
    assistant leaves a TTS voicemail when AMD detects a machine.
    """
    if settings.VAPI_API_KEY is None:
        return {
            "processed": 0,
            "started": 0,
            "failed": 0,
            "errors": [{"reason": "vapi_api_key_not_configured"}],
        }
    api_key = settings.VAPI_API_KEY.get_secret_value()

    # Atomically claim up to 10 due rows by flipping status → 'processing'.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH due AS (
                    SELECT id
                    FROM voice_callback_requests
                    WHERE status = 'scheduled'
                      AND deleted_at IS NULL
                      AND preferred_time <= NOW()
                    ORDER BY preferred_time
                    LIMIT 10
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE voice_callback_requests vcb
                SET status = 'processing', updated_at = NOW()
                FROM due
                WHERE vcb.id = due.id
                RETURNING vcb.id, vcb.brand_id, vcb.customer_number,
                          vcb.voice_assistant_id, vcb.voice_phone_number_id,
                          vcb.leave_voicemail_on_no_answer, vcb.voicemail_script
                """
            )
            claimed = await cur.fetchall()
        await conn.commit()

    processed = 0
    started = 0
    failed = 0
    errors: list[dict[str, str]] = []

    for row in claimed:
        (
            callback_id, brand_id, customer_number,
            voice_assistant_id, voice_phone_number_id,
            leave_voicemail, voicemail_script,
        ) = row
        processed += 1

        if not customer_number or voice_assistant_id is None or voice_phone_number_id is None:
            await _mark_callback(callback_id, "failed")
            errors.append({"id": str(callback_id), "reason": "missing_call_inputs"})
            failed += 1
            continue

        identifiers = await _resolve_vapi_identifiers(
            voice_assistant_id, voice_phone_number_id
        )
        if identifiers is None:
            await _mark_callback(callback_id, "failed")
            errors.append({"id": str(callback_id), "reason": "vapi_identifiers_missing"})
            failed += 1
            continue
        vapi_assistant_id, vapi_phone_number_id = identifiers

        overrides: dict[str, Any] = {}
        if leave_voicemail and voicemail_script:
            # Vapi's `assistantOverrides.voicemailMessage` is the canonical
            # field for TTS-on-AMD; we also enable Vapi-side AMD so the
            # provider drops the voicemail without a separate audio asset.
            overrides["voicemailMessage"] = voicemail_script
            overrides["voicemailDetection"] = {
                "enabled": True,
                "provider": "twilio",
                "voicemailDetectionTypes": [
                    "machine_end_beep",
                    "machine_end_silence",
                ],
            }

        try:
            result = vapi_client.create_call(
                api_key=api_key,
                assistant_id=vapi_assistant_id,
                customer_number=customer_number,
                phone_number_id=vapi_phone_number_id,
                **overrides,
            )
        except Exception as exc:
            logger.warning(
                "callback_run_vapi_create_failed",
                extra={"callback_id": str(callback_id), "error": str(exc)},
            )
            await _mark_callback(callback_id, "failed")
            errors.append({"id": str(callback_id), "reason": str(exc)[:200]})
            failed += 1
            continue

        vapi_call_id = result.get("id")
        await _mark_callback(callback_id, "completed", vapi_call_id=vapi_call_id)
        started += 1

    return {
        "processed": processed,
        "started": started,
        "failed": failed,
        "errors": errors,
    }


async def _resolve_vapi_identifiers(
    voice_assistant_id: UUID, voice_phone_number_id: UUID
) -> tuple[str, str] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT vapi_assistant_id FROM voice_assistants
                WHERE id = %s AND deleted_at IS NULL
                """,
                (str(voice_assistant_id),),
            )
            row = await cur.fetchone()
            assistant = row[0] if row else None

            await cur.execute(
                """
                SELECT vapi_phone_number_id FROM voice_phone_numbers
                WHERE id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id),),
            )
            row = await cur.fetchone()
            phone = row[0] if row else None
    if not assistant or not phone:
        return None
    return assistant, phone


async def _mark_callback(
    callback_id: UUID, status: str, *, vapi_call_id: str | None = None
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if vapi_call_id is not None:
                await cur.execute(
                    """
                    UPDATE voice_callback_requests
                    SET status = %s,
                        source_vapi_call_id = COALESCE(NULLIF(source_vapi_call_id, ''), %s),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (status, vapi_call_id, str(callback_id)),
                )
            else:
                await cur.execute(
                    """
                    UPDATE voice_callback_requests
                    SET status = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (status, str(callback_id)),
                )
        await conn.commit()
