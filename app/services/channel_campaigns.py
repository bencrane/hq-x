"""CRUD service for business.channel_campaigns.

A channel_campaign is the per-channel execution unit underneath a campaign.
Channel-specific validation lives here:
  * direct_mail — design_id required and must reference a brand-scoped
    dmaas_designs row.
  * email       — provider must currently be 'emailbison' or 'manual'; wiring
    is out of scope for this migration (status stays 'draft').
  * voice_outbound / sms — provider must match the legacy substrate (vapi vs
    twilio for voice; twilio for sms).

Brand-org consistency is enforced via the parent campaign: a channel_campaign
inherits organization_id and brand_id from its campaign, so callers cannot
accidentally attach to the wrong org/brand.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.campaigns import (
    VALID_CHANNEL_PROVIDER_PAIRS,
    ChannelCampaignCreate,
    ChannelCampaignResponse,
    ChannelCampaignStatus,
    ChannelCampaignUpdate,
)
from app.services.campaigns import (
    CampaignNotFound,
    compute_scheduled_send_at,
    get_campaign,
)


class ChannelCampaignError(Exception):
    pass


class ChannelCampaignNotFound(ChannelCampaignError):
    pass


class ChannelCampaignChannelProviderInvalid(ChannelCampaignError):
    pass


class ChannelCampaignDesignRequired(ChannelCampaignError):
    pass


class ChannelCampaignDesignBrandMismatch(ChannelCampaignError):
    pass


class ChannelCampaignInvalidStatusTransition(ChannelCampaignError):
    pass


_COLUMNS = (
    "id, campaign_id, organization_id, brand_id, name, channel, provider, "
    "audience_spec_id, audience_snapshot_count, status, start_offset_days, "
    "scheduled_send_at, schedule_config, provider_config, design_id, metadata, "
    "created_by_user_id, created_at, updated_at, archived_at"
)


def _row_to_response(row: tuple[Any, ...]) -> ChannelCampaignResponse:
    return ChannelCampaignResponse(
        id=row[0],
        campaign_id=row[1],
        organization_id=row[2],
        brand_id=row[3],
        name=row[4],
        channel=row[5],
        provider=row[6],
        audience_spec_id=row[7],
        audience_snapshot_count=row[8],
        status=row[9],
        start_offset_days=row[10],
        scheduled_send_at=row[11],
        schedule_config=row[12] or {},
        provider_config=row[13] or {},
        design_id=row[14],
        metadata=row[15] or {},
        created_by_user_id=row[16],
        created_at=row[17],
        updated_at=row[18],
        archived_at=row[19],
    )


async def _validate_design_for_brand(*, design_id: UUID, brand_id: UUID) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT brand_id
                FROM dmaas_designs
                WHERE id = %s
                """,
                (str(design_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise ChannelCampaignDesignBrandMismatch(f"design {design_id} not found")
    if row[0] != brand_id:
        raise ChannelCampaignDesignBrandMismatch(
            f"design {design_id} belongs to brand {row[0]}, not {brand_id}"
        )


def _validate_channel_provider(channel: str, provider: str) -> None:
    if (channel, provider) not in VALID_CHANNEL_PROVIDER_PAIRS:
        raise ChannelCampaignChannelProviderInvalid(
            f"({channel}, {provider}) is not a supported channel/provider pair"
        )


def _validate_channel_specific(payload: ChannelCampaignCreate) -> None:
    if payload.channel == "direct_mail" and payload.design_id is None:
        raise ChannelCampaignDesignRequired(
            "design_id is required for direct_mail channel campaigns"
        )


async def create_channel_campaign(
    *,
    organization_id: UUID,
    payload: ChannelCampaignCreate,
    created_by_user_id: UUID | None,
) -> ChannelCampaignResponse:
    _validate_channel_provider(payload.channel, payload.provider)
    _validate_channel_specific(payload)

    campaign = await get_campaign(
        campaign_id=payload.campaign_id, organization_id=organization_id
    )

    if payload.design_id is not None:
        await _validate_design_for_brand(
            design_id=payload.design_id, brand_id=campaign.brand_id
        )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.channel_campaigns
                    (campaign_id, organization_id, brand_id, name, channel,
                     provider, audience_spec_id, audience_snapshot_count,
                     start_offset_days, schedule_config, provider_config,
                     design_id, metadata, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    str(payload.campaign_id),
                    str(campaign.organization_id),
                    str(campaign.brand_id),
                    payload.name,
                    payload.channel,
                    payload.provider,
                    str(payload.audience_spec_id) if payload.audience_spec_id else None,
                    payload.audience_snapshot_count,
                    payload.start_offset_days,
                    Jsonb(payload.schedule_config),
                    Jsonb(payload.provider_config),
                    str(payload.design_id) if payload.design_id else None,
                    Jsonb(payload.metadata),
                    str(created_by_user_id) if created_by_user_id else None,
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_response(row)


async def get_channel_campaign(
    *, channel_campaign_id: UUID, organization_id: UUID
) -> ChannelCampaignResponse:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.channel_campaigns
                WHERE id = %s AND organization_id = %s
                """,
                (str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise ChannelCampaignNotFound(
            f"channel_campaign {channel_campaign_id} not found in org "
            f"{organization_id}"
        )
    return _row_to_response(row)


async def list_channel_campaigns(
    *,
    organization_id: UUID,
    campaign_id: UUID | None = None,
    channel: str | None = None,
    status: ChannelCampaignStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ChannelCampaignResponse]:
    where = ["organization_id = %s"]
    args: list[Any] = [str(organization_id)]
    if campaign_id is not None:
        where.append("campaign_id = %s")
        args.append(str(campaign_id))
    if channel is not None:
        where.append("channel = %s")
        args.append(channel)
    if status is not None:
        where.append("status = %s")
        args.append(status)
    args.extend([limit, offset])
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.channel_campaigns
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    return [_row_to_response(r) for r in rows]


async def update_channel_campaign(
    *,
    channel_campaign_id: UUID,
    organization_id: UUID,
    payload: ChannelCampaignUpdate,
) -> ChannelCampaignResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return await get_channel_campaign(
            channel_campaign_id=channel_campaign_id, organization_id=organization_id
        )

    if "design_id" in fields and fields["design_id"] is not None:
        existing = await get_channel_campaign(
            channel_campaign_id=channel_campaign_id, organization_id=organization_id
        )
        await _validate_design_for_brand(
            design_id=fields["design_id"], brand_id=existing.brand_id
        )

    set_parts: list[str] = []
    args: list[Any] = []
    json_keys = {"schedule_config", "provider_config", "metadata"}
    for key, value in fields.items():
        if key in json_keys:
            set_parts.append(f"{key} = %s")
            args.append(Jsonb(value or {}))
        elif key in ("audience_spec_id", "design_id"):
            set_parts.append(f"{key} = %s")
            args.append(str(value) if value is not None else None)
        else:
            set_parts.append(f"{key} = %s")
            args.append(value)
    set_parts.append("updated_at = NOW()")
    args.extend([str(channel_campaign_id), str(organization_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaigns
                SET {', '.join(set_parts)}
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
    if row is None:
        raise ChannelCampaignNotFound(
            f"channel_campaign {channel_campaign_id} not found in org "
            f"{organization_id}"
        )
    return _row_to_response(row)


async def activate_channel_campaign(
    *,
    channel_campaign_id: UUID,
    organization_id: UUID,
) -> ChannelCampaignResponse:
    """Transition draft → scheduled and compute scheduled_send_at.

    The parent campaign's start_date and the channel campaign's
    start_offset_days together determine when it actually fires. If the
    campaign has no start_date the column stays NULL (callers treat that
    as "send now").
    """
    cc = await get_channel_campaign(
        channel_campaign_id=channel_campaign_id, organization_id=organization_id
    )
    if cc.status not in ("draft", "paused", "failed"):
        raise ChannelCampaignInvalidStatusTransition(
            f"cannot activate from status={cc.status}"
        )
    try:
        campaign = await get_campaign(
            campaign_id=cc.campaign_id, organization_id=organization_id
        )
    except CampaignNotFound as exc:  # should be impossible given the FK
        raise ChannelCampaignError(
            f"channel_campaign {channel_campaign_id} references missing campaign"
        ) from exc

    scheduled_send_at = compute_scheduled_send_at(
        campaign_start_date=campaign.start_date,
        start_offset_days=cc.start_offset_days,
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaigns
                SET status = 'scheduled',
                    scheduled_send_at = %s,
                    updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                (scheduled_send_at, str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_response(row)


async def _set_status(
    *,
    channel_campaign_id: UUID,
    organization_id: UUID,
    new_status: ChannelCampaignStatus,
    allowed_from: tuple[str, ...],
    set_archived_at: bool = False,
) -> ChannelCampaignResponse:
    cc = await get_channel_campaign(
        channel_campaign_id=channel_campaign_id, organization_id=organization_id
    )
    if cc.status not in allowed_from:
        raise ChannelCampaignInvalidStatusTransition(
            f"cannot transition {cc.status} → {new_status}"
        )
    archive_clause = (
        ", archived_at = COALESCE(archived_at, NOW())" if set_archived_at else ""
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaigns
                SET status = %s, updated_at = NOW(){archive_clause}
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                (new_status, str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
            # When archiving, cascade to child steps so dashboards / scheduler
            # don't keep them in flight.
            if set_archived_at and new_status == "archived":
                await cur.execute(
                    """
                    UPDATE business.channel_campaign_steps
                    SET status = 'archived', updated_at = NOW()
                    WHERE channel_campaign_id = %s AND status != 'archived'
                    """,
                    (str(channel_campaign_id),),
                )
    assert row is not None
    return _row_to_response(row)


async def pause_channel_campaign(
    *, channel_campaign_id: UUID, organization_id: UUID
) -> ChannelCampaignResponse:
    return await _set_status(
        channel_campaign_id=channel_campaign_id,
        organization_id=organization_id,
        new_status="paused",
        allowed_from=("scheduled", "sending"),
    )


async def resume_channel_campaign(
    *, channel_campaign_id: UUID, organization_id: UUID
) -> ChannelCampaignResponse:
    return await _set_status(
        channel_campaign_id=channel_campaign_id,
        organization_id=organization_id,
        new_status="scheduled",
        allowed_from=("paused",),
    )


async def archive_channel_campaign(
    *, channel_campaign_id: UUID, organization_id: UUID
) -> ChannelCampaignResponse:
    return await _set_status(
        channel_campaign_id=channel_campaign_id,
        organization_id=organization_id,
        new_status="archived",
        allowed_from=("draft", "scheduled", "sending", "sent", "paused", "failed"),
        set_archived_at=True,
    )


async def get_channel_campaign_context(
    *, channel_campaign_id: UUID
) -> dict[str, Any] | None:
    """Resolve org/brand/campaign/channel/provider for analytics tagging.

    Returns None when not found. The shape matches the keys downstream
    Rudderstack/ClickHouse writes are required to carry, so callers can
    spread the dict directly into the event envelope.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT organization_id, brand_id, campaign_id, channel, provider
                FROM business.channel_campaigns
                WHERE id = %s
                """,
                (str(channel_campaign_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "organization_id": str(row[0]),
        "brand_id": str(row[1]),
        "campaign_id": str(row[2]),
        "channel_campaign_id": str(channel_campaign_id),
        "channel": row[3],
        "provider": row[4],
    }


__all__ = [
    "ChannelCampaignError",
    "ChannelCampaignNotFound",
    "ChannelCampaignChannelProviderInvalid",
    "ChannelCampaignDesignRequired",
    "ChannelCampaignDesignBrandMismatch",
    "ChannelCampaignInvalidStatusTransition",
    "create_channel_campaign",
    "get_channel_campaign",
    "list_channel_campaigns",
    "update_channel_campaign",
    "activate_channel_campaign",
    "pause_channel_campaign",
    "resume_channel_campaign",
    "archive_channel_campaign",
    "get_channel_campaign_context",
]
