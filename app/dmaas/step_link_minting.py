"""Mint one Dub.co short link per recipient on a channel_campaign_step.

Called by LobAdapter.activate_step *before* the Lob campaign object is
created. If any recipient's mint fails, the function raises and the
activation aborts — no Lob campaign exists, no print job is queued.

Idempotent via the unique partial index on
(channel_campaign_step_id, recipient_id). Re-running after a transient
failure short-circuits already-minted recipients via
find_dub_link_for_step_recipient.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.config import settings
from app.dmaas import dub_links as dub_links_repo
from app.dmaas.dub_links import DubLinkRecord
from app.observability import incr_metric
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError
from app.services.recipients import list_step_memberships

logger = logging.getLogger(__name__)


class StepLinkMintingError(Exception):
    """Aggregates a per-recipient failure surfaced from Dub or persistence."""

    def __init__(
        self,
        message: str,
        *,
        recipient_id: UUID | None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.recipient_id = recipient_id
        self.cause = cause


class DubNotConfiguredError(StepLinkMintingError):
    pass


async def mint_links_for_step(
    *,
    channel_campaign_step_id: UUID,
    organization_id: UUID,
    brand_id: UUID | None,
    campaign_id: UUID,
    channel_campaign_id: UUID,
    destination_url: str,
    created_by_user_id: UUID | None = None,
    domain: str | None = None,
    tenant_id: str | None = None,
) -> list[DubLinkRecord]:
    """Mint a Dub link for every 'pending' recipient on the step.

    Skips recipients that already have a link (idempotent retry). Raises
    StepLinkMintingError on the first persistent failure — caller is
    responsible for not proceeding to Lob.
    """
    if settings.DUB_API_KEY is None:
        raise DubNotConfiguredError(
            "DUB_API_KEY is not set", recipient_id=None
        )
    api_key = settings.DUB_API_KEY.get_secret_value()
    resolved_domain = domain or settings.DUB_DEFAULT_DOMAIN
    resolved_tenant = tenant_id or settings.DUB_DEFAULT_TENANT_ID

    memberships = await list_step_memberships(
        channel_campaign_step_id=channel_campaign_step_id,
        status="pending",
    )
    out: list[DubLinkRecord] = []

    for m in memberships:
        existing = await dub_links_repo.find_dub_link_for_step_recipient(
            channel_campaign_step_id=channel_campaign_step_id,
            recipient_id=m.recipient_id,
        )
        if existing is not None:
            out.append(existing)
            continue

        external_id = f"step:{channel_campaign_step_id}:rcpt:{m.recipient_id}"
        attribution: dict[str, Any] = {
            "campaign_id": str(campaign_id),
            "channel_campaign_id": str(channel_campaign_id),
            "channel_campaign_step_id": str(channel_campaign_step_id),
            "recipient_id": str(m.recipient_id),
            "organization_id": str(organization_id),
        }

        try:
            payload = dub_client.create_link(
                api_key=api_key,
                url=destination_url,
                domain=resolved_domain,
                external_id=external_id,
                tenant_id=resolved_tenant,
            )
        except DubProviderError as exc:
            incr_metric(
                "dub.step_mint.error",
                category=exc.category,
                status=str(exc.status) if exc.status else "none",
            )
            raise StepLinkMintingError(
                f"dub create_link failed for recipient {m.recipient_id}",
                recipient_id=m.recipient_id,
                cause=exc,
            ) from exc

        try:
            record = await dub_links_repo.insert_dub_link(
                dub_link_id=str(payload.get("id", "")),
                dub_external_id=payload.get("externalId"),
                dub_short_url=str(
                    payload.get("shortLink") or payload.get("url") or ""
                ),
                dub_domain=str(payload.get("domain", "")),
                dub_key=str(payload.get("key", "")),
                destination_url=str(payload.get("url") or destination_url),
                dmaas_design_id=None,
                direct_mail_piece_id=None,
                brand_id=brand_id,
                attribution_context=attribution,
                created_by_user_id=created_by_user_id,
                channel_campaign_step_id=channel_campaign_step_id,
                recipient_id=m.recipient_id,
            )
        except Exception as exc:
            incr_metric("dub.step_mint.persist_failed")
            logger.exception(
                "dmaas_dub_links insert failed for step=%s recipient=%s",
                channel_campaign_step_id,
                m.recipient_id,
            )
            raise StepLinkMintingError(
                f"persistence failed for recipient {m.recipient_id} "
                f"(dub link {payload.get('id')} now orphaned upstream)",
                recipient_id=m.recipient_id,
                cause=exc,
            ) from exc

        incr_metric("dub.step_mint.created")
        out.append(record)

    return out


__all__ = [
    "mint_links_for_step",
    "StepLinkMintingError",
    "DubNotConfiguredError",
]
