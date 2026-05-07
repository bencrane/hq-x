"""Cluster 3 EmailBison send seam — POST /api/replies/new (compose new email).

Used by the Cluster 3 dispatcher to send the intro as a NEW thread to
the supply-side recipient. We deliberately do NOT use
``/api/replies/{reply_id}/reply`` (in-thread) — operator chose new-thread
because if the original supply-side reply was hostile, threading is
worse risk-wise.

Two modes, gated by ``settings.CLUSTER3_LIVE_SEND``:

* live (CLUSTER3_LIVE_SEND truthy) — actually POST /api/replies/new
  via ``providers.emailbison.client``. Real send.
* dry-run (CLUSTER3_LIVE_SEND falsy, the simulation default) — no network
  call. Returns a deterministic fake ``eb_reply_id`` and the would-have-
  sent payload so the orchestrator can stamp the email_messages /
  lead_transfers rows as if a send happened.

This module is intentionally narrow. It does NOT touch the DB; the
caller (cluster3_dispatch) writes the email_messages and
lead_transfers rows around the send.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError

logger = logging.getLogger(__name__)


class ClusterIntroSendError(Exception):
    pass


def _live_send_enabled() -> bool:
    flag = getattr(settings, "CLUSTER3_LIVE_SEND", False)
    if isinstance(flag, str):
        return flag.lower() in ("1", "true", "yes", "on")
    return bool(flag)


def _api_key_or_raise() -> str:
    secret = getattr(settings, "EMAILBISON_API_KEY", None)
    if secret is None:
        raise ClusterIntroSendError("EMAILBISON_API_KEY not configured")
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    return str(secret)


async def send_intro(
    *,
    sender_email_id: int | str | None,
    to_email: str,
    to_name: str | None,
    cc_emails: list[str] | None,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send the Cluster 3 intro. Returns ``{eb_reply_id, mode, payload, response}``.

    ``mode`` is ``'live'`` when the real EB endpoint was hit, ``'dry_run'``
    otherwise. ``payload`` is the EB request body that was (or would have
    been) sent — operator-readable for forensic replay.
    """
    payload: dict[str, Any] = {
        "to_email": to_email,
        "subject": subject,
        "text_body": body_text,
    }
    if to_name:
        payload["to_name"] = to_name
    if body_html:
        payload["html_body"] = body_html
    if cc_emails:
        payload["cc_emails"] = list(cc_emails)
    if sender_email_id is not None:
        payload["sender_email_id"] = sender_email_id

    mode = "live" if _live_send_enabled() else "dry_run"

    if mode == "dry_run":
        fake_id = int(time.time() * 1000)
        logger.info(
            "eb_send.send_intro DRY-RUN to=%s subject=%s fake_id=%s",
            to_email,
            subject,
            fake_id,
        )
        return {
            "eb_reply_id": fake_id,
            "mode": "dry_run",
            "payload": payload,
            "response": {
                "id": fake_id,
                "subject": subject,
                "to_email": to_email,
                "dry_run": True,
                "metadata_echo": metadata or {},
            },
        }

    api_key = _api_key_or_raise()
    try:
        response = eb_client._request_json(
            api_key=api_key,
            method="POST",
            path="/api/replies/new",
            json_payload=payload,
        )
    except EmailBisonProviderError as exc:
        raise ClusterIntroSendError(
            f"emailbison /api/replies/new failed: {exc}"
        ) from exc

    eb_reply_id = None
    if isinstance(response, dict):
        eb_reply_id = response.get("id") or (response.get("data") or {}).get("id")

    return {
        "eb_reply_id": eb_reply_id,
        "mode": "live",
        "payload": payload,
        "response": response,
    }


__all__ = ["send_intro", "ClusterIntroSendError"]
