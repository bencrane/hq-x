"""SMS send service.

Drift fix §7.3: every send checks sms_suppressions(brand_id, to_number) first.
If the recipient is suppressed, raise SmsSuppressedError — do not call Twilio.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import send_message

logger = logging.getLogger(__name__)


class SmsSuppressedError(Exception):
    """Recipient is on the brand's sms_suppressions list."""


# OEX-style STOP keyword set, case-insensitive, body-trimmed.
STOP_KEYWORD_RE = re.compile(
    r"^\s*(stop|stopall|unsubscribe|end|quit|cancel)\s*$",
    flags=re.IGNORECASE,
)


def _api_base_url() -> str:
    return settings.HQX_API_BASE_URL.rstrip("/")


async def _is_suppressed(brand_id: UUID, phone_number: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM sms_suppressions
                WHERE brand_id = %s AND phone_number = %s
                LIMIT 1
                """,
                (str(brand_id), phone_number),
            )
            row = await cur.fetchone()
    return row is not None


async def add_suppression(
    brand_id: UUID,
    phone_number: str,
    *,
    reason: str = "stop_keyword",
    notes: str | None = None,
) -> None:
    """Idempotent: ON CONFLICT DO NOTHING via composite UNIQUE."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sms_suppressions (brand_id, phone_number, reason, notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (brand_id, phone_number) DO NOTHING
                """,
                (str(brand_id), phone_number, reason, notes),
            )
        await conn.commit()


async def send_sms(
    *,
    brand_id: UUID,
    account_sid: str,
    auth_token: str,
    to: str,
    body: str | None = None,
    from_number: str | None = None,
    messaging_service_sid: str | None = None,
    media_url: list[str] | None = None,
    partner_id: UUID | None = None,
    campaign_id: UUID | None = None,
) -> dict[str, Any]:
    """Send an SMS/MMS message via Twilio.

    Suppression check (§7.3) is enforced before any provider call.
    """
    if await _is_suppressed(brand_id, to):
        logger.info(
            "sms_send_suppressed",
            extra={"brand_id": str(brand_id), "to": to},
        )
        raise SmsSuppressedError(f"phone number {to} is suppressed for brand {brand_id}")

    api_base = _api_base_url()
    status_callback = f"{api_base}/api/webhooks/twilio/{brand_id}"

    if not from_number and not messaging_service_sid:
        raise ValueError("must provide from_number or messaging_service_sid")

    try:
        result = send_message(
            account_sid,
            auth_token,
            to=to,
            body=body,
            from_number=from_number,
            messaging_service_sid=messaging_service_sid,
            media_url=media_url,
            status_callback=status_callback,
        )
    except TwilioProviderError:
        logger.warning(
            "sms_send_failed",
            extra={"brand_id": str(brand_id), "to": to},
        )
        raise

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sms_messages (
                    brand_id, partner_id, campaign_id, message_sid, account_sid,
                    messaging_service_sid, direction, from_number, to_number,
                    body, num_segments, num_media, media_urls, status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'outbound-api',
                    %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    str(brand_id),
                    str(partner_id) if partner_id else None,
                    str(campaign_id) if campaign_id else None,
                    result["sid"],
                    account_sid,
                    result.get("messaging_service_sid"),
                    result.get("from", from_number or ""),
                    to,
                    body,
                    int(result.get("num_segments")) if result.get("num_segments") else None,
                    int(result.get("num_media")) if result.get("num_media") else None,
                    json.dumps(media_url) if media_url else None,
                    result.get("status", "queued"),
                ),
            )
        await conn.commit()

    return {
        "message_sid": result["sid"],
        "status": result.get("status", "queued"),
        "direction": "outbound-api",
        "from_number": result.get("from", from_number or ""),
        "to": to,
    }


async def update_status_from_callback(
    *,
    message_sid: str,
    new_status: str,
    error_code: int | None = None,
    callback_payload: dict[str, Any] | None = None,
) -> bool:
    """Drift fix §7.2: update sms_messages.status from a Twilio status callback.

    Returns True if a row was found and updated, False otherwise.
    """
    set_parts = ["status = %s", "updated_at = NOW()"]
    values: list[Any] = [new_status]
    if error_code is not None:
        set_parts.append("error_code = %s")
        values.append(error_code)
    if callback_payload is not None:
        set_parts.append("last_callback_payload = %s::jsonb")
        values.append(json.dumps(callback_payload))
    values.append(message_sid)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE sms_messages
                SET {", ".join(set_parts)}
                WHERE message_sid = %s
                RETURNING id
                """,
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    return row is not None


async def record_inbound_sms(
    *,
    brand_id: UUID,
    message_sid: str,
    account_sid: str,
    from_number: str,
    to_number: str,
    body: str | None,
    payload: dict[str, Any],
) -> UUID:
    """Insert an inbound SMS row. Returns the row id."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sms_messages (
                    brand_id, message_sid, account_sid, direction,
                    from_number, to_number, body, status, last_callback_payload
                )
                VALUES (%s, %s, %s, 'inbound', %s, %s, %s, 'received', %s::jsonb)
                ON CONFLICT (message_sid) DO UPDATE SET
                    last_callback_payload = EXCLUDED.last_callback_payload,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    str(brand_id), message_sid, account_sid,
                    from_number, to_number, body,
                    json.dumps(payload),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row[0]


def is_stop_keyword(body: str | None) -> bool:
    if not body:
        return False
    return STOP_KEYWORD_RE.match(body) is not None
