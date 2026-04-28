"""Outbound calls service.

Brand-axis port of OEX ``services/outbound_calls.py``. Critical change: OEX
pre-created a ``voice_sessions`` row then wrote ``outbound_call_configs``
with ``voice_session_id`` FK. In hq-x, ``voice_sessions`` is folded into
``call_logs`` and ``outbound_call_configs.call_log_id`` FKs to ``call_logs(id)``.

Pattern preserved:
  1. Pre-create call_logs row with placeholder twilio_call_sid.
  2. Insert outbound_call_configs with call_log_id FK.
  3. Build TwiML connect URL + status-callback URL.
  4. Call Twilio create_call. On failure, delete both pre-created rows.
  5. Update call_logs.twilio_call_sid with the real SID.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import create_call

logger = logging.getLogger(__name__)


def _api_base_url() -> str:
    base = settings.HQX_API_BASE_URL or ""
    s = str(base).rstrip("/")
    if not s:
        raise ValueError("HQX_API_BASE_URL must be configured for outbound calling")
    return s


async def initiate_outbound_call(
    *,
    brand_id: UUID,
    account_sid: str,
    auth_token: str,
    to: str,
    from_number: str,
    greeting_text: str | None = None,
    voicemail_text: str | None = None,
    voicemail_audio_url: str | None = None,
    human_message_text: str | None = None,
    record: bool = False,
    timeout: int = 30,
    partner_id: UUID | None = None,
    campaign_id: UUID | None = None,
    campaign_lead_id: str | None = None,
    amd_strategy: str | None = None,
    vapi_assistant_id: str | None = None,
    vapi_sip_uri: str | None = None,
) -> dict[str, Any]:
    """Initiate an outbound call via Twilio. Returns identifiers + status."""
    api_base = _api_base_url()
    twiml_token = secrets.token_hex(16)
    call_log_id = uuid4()
    placeholder_sid = f"pending-{call_log_id}"

    metadata: dict[str, Any] = {}
    if campaign_lead_id:
        metadata["campaign_lead_id"] = campaign_lead_id

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # 1. Pre-create call_logs row
            await cur.execute(
                """
                INSERT INTO call_logs (
                    id, brand_id, partner_id, campaign_id,
                    twilio_call_sid, direction, call_type,
                    customer_number, from_number, status, metadata
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, 'outbound', 'outbound',
                    %s, %s, 'queued', %s::jsonb
                )
                """,
                (
                    str(call_log_id), str(brand_id),
                    str(partner_id) if partner_id else None,
                    str(campaign_id) if campaign_id else None,
                    placeholder_sid,
                    to, from_number,
                    __import__("json").dumps(metadata) if metadata else None,
                ),
            )

            # 2. Insert outbound_call_configs
            await cur.execute(
                """
                INSERT INTO outbound_call_configs (
                    brand_id, call_log_id, twiml_token,
                    greeting_text, voicemail_text, voicemail_audio_url,
                    human_message_text, from_number,
                    amd_strategy, vapi_assistant_id, vapi_sip_uri
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(brand_id), str(call_log_id), twiml_token,
                    greeting_text, voicemail_text, voicemail_audio_url,
                    human_message_text, from_number,
                    amd_strategy, vapi_assistant_id, vapi_sip_uri,
                ),
            )
        await conn.commit()

    twiml_connect_url = (
        f"{api_base}/api/voice/outbound/twiml/connect/{call_log_id}?token={twiml_token}"
    )
    webhook_url = f"{api_base}/api/webhooks/twilio/{brand_id}"

    try:
        result = create_call(
            account_sid,
            auth_token,
            to=to,
            from_number=from_number,
            url=twiml_connect_url,
            status_callback=webhook_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            machine_detection="DetectMessageEnd",
            async_amd=True,
            async_amd_status_callback=webhook_url,
            record=record,
            recording_status_callback=webhook_url if record else None,
            timeout=timeout,
        )
    except TwilioProviderError:
        # Clean up pre-created rows on failure
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM outbound_call_configs WHERE call_log_id = %s",
                    (str(call_log_id),),
                )
                await cur.execute(
                    "DELETE FROM call_logs WHERE id = %s",
                    (str(call_log_id),),
                )
            await conn.commit()
        raise

    real_sid = result.get("sid", "")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE call_logs
                SET twilio_call_sid = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (real_sid, str(call_log_id)),
            )
        await conn.commit()

    logger.info(
        "outbound_call_initiated brand=%s call_log_id=%s call_sid=%s to=%s",
        brand_id, call_log_id, real_sid, to,
    )

    return {
        "call_sid": real_sid,
        "call_log_id": str(call_log_id),
        "status": "initiated",
        "direction": "outbound",
        "from_number": from_number,
        "to": to,
    }
