"""Dub-specific helpers on business.channel_campaigns.

Reads / writes the `dub_folder_id` column added in migration
20260430T140000_channel_campaigns_dub_folder.sql.

Two concurrent activations of different steps in the same campaign could
both see dub_folder_id IS NULL and both create a folder. The
SELECT … FOR UPDATE pattern in `acquire_or_set_dub_folder_id` serializes
the read+write so only one creates and the other reads back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from app.db import get_db_connection


async def get_dub_folder_id(channel_campaign_id: UUID) -> str | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT dub_folder_id FROM business.channel_campaigns WHERE id = %s",
            (str(channel_campaign_id),),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def set_dub_folder_id(
    *,
    channel_campaign_id: UUID,
    dub_folder_id: str,
) -> None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE business.channel_campaigns SET dub_folder_id = %s, "
            "updated_at = NOW() WHERE id = %s",
            (dub_folder_id, str(channel_campaign_id)),
        )


async def acquire_or_set_dub_folder_id(
    *,
    channel_campaign_id: UUID,
    create_folder: Callable[[], Awaitable[str]],
) -> str:
    """SELECT … FOR UPDATE to serialize concurrent activations.

    If `dub_folder_id` is already set, return it. Otherwise call
    `create_folder()` (which is expected to create the folder via Dub) and
    persist the returned id within the same transaction.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT dub_folder_id FROM business.channel_campaigns "
                    "WHERE id = %s FOR UPDATE",
                    (str(channel_campaign_id),),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"channel_campaign {channel_campaign_id} not found"
                    )
                existing = row[0]
                if existing:
                    return existing

                folder_id = await create_folder()
                await cur.execute(
                    "UPDATE business.channel_campaigns SET dub_folder_id = %s, "
                    "updated_at = NOW() WHERE id = %s",
                    (folder_id, str(channel_campaign_id)),
                )
                return folder_id
