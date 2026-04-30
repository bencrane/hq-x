"""DB read/write for business.landing_page_views.

One row per server-side render that resolves a recipient. Joined into
the recipient timeline (Slice 5) alongside dmaas_dub_events and
direct_mail_piece_events so dashboards can show the full
"delivered → clicked → viewed → submitted" funnel per recipient.

Source-IP material is hashed before persistence (raw IP never lands in
the row). Hashing happens at the call site — this repo only persists
whatever `source_metadata` dict the caller hands it.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


@dataclass(frozen=True)
class LandingPageViewRecord:
    id: UUID
    organization_id: UUID
    brand_id: UUID
    campaign_id: UUID
    channel_campaign_id: UUID
    channel_campaign_step_id: UUID
    recipient_id: UUID
    source_metadata: dict[str, Any] | None
    viewed_at: datetime


async def insert_view(
    *,
    organization_id: UUID,
    brand_id: UUID,
    campaign_id: UUID,
    channel_campaign_id: UUID,
    channel_campaign_step_id: UUID,
    recipient_id: UUID,
    source_metadata: dict[str, Any] | None = None,
) -> LandingPageViewRecord:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.landing_page_views (
                    organization_id, brand_id, campaign_id,
                    channel_campaign_id, channel_campaign_step_id,
                    recipient_id, source_metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING
                    id, organization_id, brand_id, campaign_id,
                    channel_campaign_id, channel_campaign_step_id,
                    recipient_id, source_metadata, viewed_at
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    str(campaign_id),
                    str(channel_campaign_id),
                    str(channel_campaign_step_id),
                    str(recipient_id),
                    None if source_metadata is None else _json.dumps(source_metadata),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return LandingPageViewRecord(
        id=row[0],
        organization_id=row[1],
        brand_id=row[2],
        campaign_id=row[3],
        channel_campaign_id=row[4],
        channel_campaign_step_id=row[5],
        recipient_id=row[6],
        source_metadata=row[7],
        viewed_at=row[8],
    )


async def has_recent_view_for_ip(
    *,
    channel_campaign_step_id: UUID,
    ip_hash: str,
    within_seconds: int,
) -> bool:
    """Has this hashed IP rendered this step within the last N seconds?

    Used by the render path to deduplicate `page.viewed` emits when a
    recipient hits refresh — we still serve the page, but skip the
    second `emit_event` and skip the second insert.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM business.landing_page_views
            WHERE channel_campaign_step_id = %s
              AND viewed_at >= NOW() - (%s || ' seconds')::interval
              AND source_metadata ->> 'ip_hash' = %s
            LIMIT 1
            """,
            (str(channel_campaign_step_id), str(within_seconds), ip_hash),
        )
        row = await cur.fetchone()
    return row is not None


__all__ = [
    "LandingPageViewRecord",
    "has_recent_view_for_ip",
    "insert_view",
]
