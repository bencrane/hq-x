"""CRUD service for business.campaigns (the umbrella outreach effort).

A campaign is org-scoped, brand-bound, and channel-agnostic. It has 1..N
channel_campaigns under it (see ``app/services/channel_campaigns.py``).

Brand-org consistency is enforced here (the brand must belong to the
supplied organization), since DB-level FKs only enforce existence, not
the org→brand relationship.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.campaigns import (
    CampaignCreate,
    CampaignResponse,
    CampaignStatus,
    CampaignUpdate,
)


class CampaignError(Exception):
    """Base error for campaign service failures."""


class CampaignNotFound(CampaignError):
    pass


class CampaignBrandMismatch(CampaignError):
    """Brand does not belong to the supplied organization."""


class CampaignInvalidStatusTransition(CampaignError):
    pass


_COLUMNS = (
    "id, organization_id, brand_id, name, description, status, start_date, "
    "metadata, created_by_user_id, created_at, updated_at, archived_at"
)


def _row_to_response(row: tuple[Any, ...]) -> CampaignResponse:
    return CampaignResponse(
        id=row[0],
        organization_id=row[1],
        brand_id=row[2],
        name=row[3],
        description=row[4],
        status=row[5],
        start_date=row[6],
        metadata=row[7] or {},
        created_by_user_id=row[8],
        created_at=row[9],
        updated_at=row[10],
        archived_at=row[11],
    )


async def _assert_brand_in_org(
    *, brand_id: UUID, organization_id: UUID
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM business.brands
                WHERE id = %s AND organization_id = %s
                LIMIT 1
                """,
                (str(brand_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise CampaignBrandMismatch(
            f"brand {brand_id} is not in organization {organization_id}"
        )


async def create_campaign(
    *,
    organization_id: UUID,
    payload: CampaignCreate,
    created_by_user_id: UUID | None,
) -> CampaignResponse:
    await _assert_brand_in_org(
        brand_id=payload.brand_id, organization_id=organization_id
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.campaigns
                    (organization_id, brand_id, name, description,
                     start_date, metadata, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    str(organization_id),
                    str(payload.brand_id),
                    payload.name,
                    payload.description,
                    payload.start_date,
                    Jsonb(payload.metadata),
                    str(created_by_user_id) if created_by_user_id else None,
                ),
            )
            row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return _row_to_response(row)


async def get_campaign(
    *, campaign_id: UUID, organization_id: UUID
) -> CampaignResponse:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.campaigns
                WHERE id = %s AND organization_id = %s
                """,
                (str(campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise CampaignNotFound(
            f"campaign {campaign_id} not found in org {organization_id}"
        )
    return _row_to_response(row)


async def list_campaigns(
    *,
    organization_id: UUID,
    brand_id: UUID | None = None,
    status: CampaignStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CampaignResponse]:
    where = ["organization_id = %s"]
    args: list[Any] = [str(organization_id)]
    if brand_id is not None:
        where.append("brand_id = %s")
        args.append(str(brand_id))
    if status is not None:
        where.append("status = %s")
        args.append(status)
    args.extend([limit, offset])
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.campaigns
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    return [_row_to_response(r) for r in rows]


async def update_campaign(
    *,
    campaign_id: UUID,
    organization_id: UUID,
    payload: CampaignUpdate,
) -> CampaignResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return await get_campaign(
            campaign_id=campaign_id, organization_id=organization_id
        )

    set_parts: list[str] = []
    args: list[Any] = []
    for key, value in fields.items():
        if key == "metadata":
            set_parts.append(f"{key} = %s")
            args.append(Jsonb(value or {}))
        else:
            set_parts.append(f"{key} = %s")
            args.append(value)
    set_parts.append("updated_at = NOW()")
    args.extend([str(campaign_id), str(organization_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.campaigns
                SET {', '.join(set_parts)}
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
    if row is None:
        raise CampaignNotFound(
            f"campaign {campaign_id} not found in org {organization_id}"
        )
    return _row_to_response(row)


async def archive_campaign(
    *,
    campaign_id: UUID,
    organization_id: UUID,
) -> CampaignResponse:
    """Archive a campaign and cascade status='archived' to its child
    channel_campaigns."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.campaigns
                SET status = 'archived',
                    archived_at = COALESCE(archived_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                (str(campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
            if row is None:
                raise CampaignNotFound(
                    f"campaign {campaign_id} not found in org {organization_id}"
                )
            await cur.execute(
                """
                UPDATE business.channel_campaigns
                SET status = 'archived',
                    archived_at = COALESCE(archived_at, NOW()),
                    updated_at = NOW()
                WHERE campaign_id = %s AND status != 'archived'
                """,
                (str(campaign_id),),
            )
    return _row_to_response(row)


def compute_scheduled_send_at(
    *,
    campaign_start_date: date | None,
    start_offset_days: int,
) -> datetime | None:
    """Resolve scheduled_send_at = campaign.start_date + offset (UTC midnight).

    Returns None when the campaign has no start_date — channel campaigns can
    still be activated, but the scheduler treats them as 'send immediately'.
    """
    if campaign_start_date is None:
        return None
    from datetime import UTC, time, timedelta

    base = datetime.combine(campaign_start_date, time(0, 0), tzinfo=UTC)
    return base + timedelta(days=start_offset_days)


__all__ = [
    "CampaignError",
    "CampaignNotFound",
    "CampaignBrandMismatch",
    "CampaignInvalidStatusTransition",
    "create_campaign",
    "get_campaign",
    "list_campaigns",
    "update_campaign",
    "archive_campaign",
    "compute_scheduled_send_at",
]
