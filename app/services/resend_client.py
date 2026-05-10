"""Minimal Resend HTTP client.

One function: send a plaintext email. Resend's REST API is small and
stable enough that wrapping their SDK isn't worth the dependency.

Returns the Resend message id on success. Raises ResendError on any
non-2xx — the caller decides whether to swallow (fire-and-forget on
webhooks) or surface (operator-triggered sends).
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ResendError(Exception):
    pass


_RESEND_URL = "https://api.resend.com/emails"


async def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    reply_to: str | None = None,
) -> str:
    if settings.RESEND_API_KEY is None:
        raise ResendError("RESEND_API_KEY not configured")
    body: dict[str, object] = {
        "from": settings.RESEND_FROM_ADDRESS,
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if reply_to:
        body["reply_to"] = reply_to
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY.get_secret_value()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(_RESEND_URL, json=body, headers=headers)
    if resp.status_code >= 400:
        raise ResendError(f"resend {resp.status_code}: {resp.text}")
    msg_id = resp.json().get("id")
    logger.info("resend_send to=%s subject=%r id=%s", to, subject, msg_id)
    return msg_id or ""
