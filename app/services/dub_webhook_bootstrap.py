"""Idempotent registration of our Dub webhook receiver.

Operator-runnable (via POST /api/v1/dub/webhooks/bootstrap or as a script).
Deliberately NOT called from app startup — auto-registration on every cold
start would create webhook churn during deploys.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.config import settings
from app.dmaas import dub_webhooks_repo
from app.dmaas.dub_webhooks_repo import DubWebhookRecord
from app.providers.dub import client as dub_client

logger = logging.getLogger(__name__)

DEFAULT_TRIGGERS = ["link.clicked", "lead.created", "sale.created"]


def _hash_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def ensure_webhook_registered(
    *,
    receiver_url: str,
    triggers: list[str] | None = None,
    environment: str | None = None,
    name: str | None = None,
) -> DubWebhookRecord:
    """Create the Dub webhook + local mirror row if no active row exists for
    (environment, receiver_url); otherwise return the existing record.
    """
    env = environment or settings.APP_ENV
    triggers = triggers or DEFAULT_TRIGGERS
    name = name or f"hq-x:{env}"

    existing = await dub_webhooks_repo.find_active_for_receiver(
        environment=env, receiver_url=receiver_url
    )
    if existing is not None:
        return existing

    if settings.DUB_API_KEY is None:
        raise RuntimeError(
            "DUB_API_KEY is not set; cannot register webhook with Dub"
        )
    api_key = settings.DUB_API_KEY.get_secret_value()
    base_url = settings.DUB_API_BASE_URL
    secret = (
        settings.DUB_WEBHOOK_SECRET.get_secret_value()
        if settings.DUB_WEBHOOK_SECRET
        else None
    )

    payload: dict[str, Any] = dub_client.create_webhook(
        api_key=api_key,
        name=name,
        url=receiver_url,
        triggers=triggers,
        secret=secret,
        base_url=base_url,
    )
    dub_id = str(payload.get("id") or "")
    if not dub_id:
        raise RuntimeError("Dub create_webhook returned no id")

    return await dub_webhooks_repo.insert_dub_webhook(
        dub_webhook_id=dub_id,
        name=name,
        receiver_url=receiver_url,
        triggers=triggers,
        environment=env,
        secret_hash=_hash_secret(secret),
    )


__all__ = ["ensure_webhook_registered", "DEFAULT_TRIGGERS"]
