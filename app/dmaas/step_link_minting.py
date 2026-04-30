"""Mint Dub.co short links for every recipient on a channel_campaign_step.

Called by LobAdapter.activate_step *before* the Lob campaign object is
created. If any recipient's mint fails, the function raises and the
activation aborts — no Lob campaign exists, no print job is queued.

Bulk-first: links are minted in chunks of up to 100 per HTTP call against
POST /links/bulk so a campaign of thousands of mailers doesn't fan out
into thousands of sequential round-trips. Each chunk's response array is
walked entry-by-entry — Dub returns either a link object or
`{"error": {…}}` per input. We map the failed entry back to its
recipient_id via the input index and surface that as
StepLinkMintingError.

Idempotent via the unique partial index on
(channel_campaign_step_id, recipient_id). Re-running after a transient
failure picks up exactly where the previous run left off (already-minted
recipients are detected via a single existence query at the top, and
ON CONFLICT DO NOTHING is the safety net for racing inserts).
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
from app.services import channel_campaigns_dub
from app.services.recipients import list_step_memberships

logger = logging.getLogger(__name__)

_BULK_CHUNK_SIZE = 100


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


def _chunk(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


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
    base_url = settings.DUB_API_BASE_URL

    folder_id = await _resolve_folder_id(
        api_key=api_key,
        channel_campaign_id=channel_campaign_id,
        base_url=base_url,
    )

    memberships = await list_step_memberships(
        channel_campaign_step_id=channel_campaign_step_id,
        status="pending",
    )
    if not memberships:
        return []

    existing_rows = await dub_links_repo.list_dub_links_for_step(
        channel_campaign_step_id
    )
    existing_by_recipient: dict[UUID, DubLinkRecord] = {
        r.recipient_id: r for r in existing_rows if r.recipient_id is not None
    }

    missing = [
        m for m in memberships if m.recipient_id not in existing_by_recipient
    ]
    if not missing:
        return [existing_by_recipient[m.recipient_id] for m in memberships]

    tag_names_for = lambda: [  # noqa: E731
        f"step:{channel_campaign_step_id}",
        f"campaign:{channel_campaign_id}",
        *( [f"brand:{brand_id}"] if brand_id is not None else [] ),
    ]

    specs: list[dict[str, Any]] = []
    spec_recipients: list[UUID] = []
    spec_attribution: list[dict[str, Any]] = []
    for m in missing:
        external_id = (
            f"step:{channel_campaign_step_id}:rcpt:{m.recipient_id}"
        )
        spec: dict[str, Any] = {
            "url": destination_url,
            "external_id": external_id,
            "tag_names": tag_names_for(),
            "utm_source": "dub",
            "utm_medium": "direct_mail",
            "utm_campaign": str(channel_campaign_id),
        }
        if resolved_domain is not None:
            spec["domain"] = resolved_domain
        if resolved_tenant is not None:
            spec["tenant_id"] = resolved_tenant
        if folder_id is not None:
            spec["folder_id"] = folder_id
        specs.append(spec)
        spec_recipients.append(m.recipient_id)
        spec_attribution.append(
            {
                "campaign_id": str(campaign_id),
                "channel_campaign_id": str(channel_campaign_id),
                "channel_campaign_step_id": str(channel_campaign_step_id),
                "recipient_id": str(m.recipient_id),
                "organization_id": str(organization_id),
            }
        )

    insert_rows: list[dict[str, Any]] = []
    for chunk_start in range(0, len(specs), _BULK_CHUNK_SIZE):
        chunk = specs[chunk_start : chunk_start + _BULK_CHUNK_SIZE]
        try:
            response = dub_client.bulk_create_links(
                api_key=api_key,
                links=chunk,
                base_url=base_url,
            )
        except DubProviderError as exc:
            incr_metric(
                "dub.step_mint.bulk_error",
                category=exc.category,
                status=str(exc.status) if exc.status else "none",
            )
            raise StepLinkMintingError(
                f"dub bulk_create_links failed for step {channel_campaign_step_id}",
                recipient_id=spec_recipients[chunk_start]
                if chunk_start < len(spec_recipients)
                else None,
                cause=exc,
            ) from exc

        if not isinstance(response, list) or len(response) != len(chunk):
            raise StepLinkMintingError(
                f"dub bulk response length mismatch ({len(response)} vs {len(chunk)})",
                recipient_id=None,
            )

        for offset, entry in enumerate(response):
            global_idx = chunk_start + offset
            recipient_id = spec_recipients[global_idx]
            attribution = spec_attribution[global_idx]
            if not isinstance(entry, dict):
                raise StepLinkMintingError(
                    f"unexpected bulk entry type for recipient {recipient_id}",
                    recipient_id=recipient_id,
                )
            err = entry.get("error")
            if err is not None:
                err_msg = (
                    err.get("message")
                    if isinstance(err, dict)
                    else str(err)
                )
                incr_metric("dub.step_mint.entry_error")
                # Persist any successfully-minted rows from prior chunks so the
                # next launch sees them and resumes from the failure point.
                if insert_rows:
                    try:
                        await dub_links_repo.bulk_insert_dub_links(insert_rows)
                    except Exception:
                        logger.exception(
                            "dmaas_dub_links bulk insert failed mid-recovery for "
                            "step=%s",
                            channel_campaign_step_id,
                        )
                raise StepLinkMintingError(
                    f"dub create_link failed for recipient {recipient_id}: "
                    f"{err_msg}",
                    recipient_id=recipient_id,
                )

            tag_ids = [
                t.get("id")
                for t in (entry.get("tags") or [])
                if isinstance(t, dict) and t.get("id")
            ]
            insert_rows.append(
                {
                    "dub_link_id": str(entry.get("id", "")),
                    "dub_external_id": entry.get("externalId"),
                    "dub_short_url": str(
                        entry.get("shortLink") or entry.get("url") or ""
                    ),
                    "dub_domain": str(entry.get("domain", "")),
                    "dub_key": str(entry.get("key", "")),
                    "destination_url": str(
                        entry.get("url") or destination_url
                    ),
                    "dmaas_design_id": None,
                    "direct_mail_piece_id": None,
                    "brand_id": brand_id,
                    "channel_campaign_step_id": channel_campaign_step_id,
                    "recipient_id": recipient_id,
                    "dub_folder_id": entry.get("folderId") or folder_id,
                    "dub_tag_ids": tag_ids,
                    "attribution_context": attribution,
                    "created_by_user_id": created_by_user_id,
                }
            )

    try:
        await dub_links_repo.bulk_insert_dub_links(insert_rows)
    except Exception as exc:
        incr_metric("dub.step_mint.persist_failed")
        logger.exception(
            "dmaas_dub_links bulk insert failed for step=%s",
            channel_campaign_step_id,
        )
        raise StepLinkMintingError(
            f"persistence failed for step {channel_campaign_step_id}",
            recipient_id=None,
            cause=exc,
        ) from exc

    incr_metric("dub.step_mint.created", count=str(len(insert_rows)))

    final = await dub_links_repo.list_dub_links_for_step(channel_campaign_step_id)
    by_recipient: dict[UUID, DubLinkRecord] = {
        r.recipient_id: r for r in final if r.recipient_id is not None
    }
    return [
        by_recipient[m.recipient_id]
        for m in memberships
        if m.recipient_id in by_recipient
    ]


async def _resolve_folder_id(
    *,
    api_key: str,
    channel_campaign_id: UUID,
    base_url: str | None,
) -> str | None:
    """Look up the campaign's Dub folder, creating it on first use.

    Wrapped in SELECT … FOR UPDATE to serialize concurrent step launches in
    the same channel_campaign so they don't both try to create the folder.
    """

    async def _create() -> str:
        try:
            payload = dub_client.create_folder(
                api_key=api_key,
                name=f"campaign:{channel_campaign_id}",
                base_url=base_url,
            )
        except DubProviderError as exc:
            incr_metric(
                "dub.step_mint.folder_error",
                category=exc.category,
                status=str(exc.status) if exc.status else "none",
            )
            raise
        folder_id = str(payload.get("id") or "")
        if not folder_id:
            raise DubProviderError("Dub create_folder returned no id")
        incr_metric("dub.step_mint.folder_created")
        return folder_id

    try:
        return await channel_campaigns_dub.acquire_or_set_dub_folder_id(
            channel_campaign_id=channel_campaign_id,
            create_folder=_create,
        )
    except DubProviderError as exc:
        raise StepLinkMintingError(
            f"dub create_folder failed for channel_campaign {channel_campaign_id}",
            recipient_id=None,
            cause=exc,
        ) from exc


__all__ = [
    "mint_links_for_step",
    "StepLinkMintingError",
    "DubNotConfiguredError",
]
