"""DB read/write for dmaas_dub_links — the join table mapping a Dub short
link to the DMaaS design / direct_mail piece / brand it represents.

Thin layer over psycopg, mirrors `app/dmaas/repository.py` style.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection


@dataclass
class DubLinkRecord:
    id: UUID
    dub_link_id: str
    dub_external_id: str | None
    dub_short_url: str
    dub_domain: str
    dub_key: str
    destination_url: str
    dmaas_design_id: UUID | None
    direct_mail_piece_id: UUID | None
    brand_id: UUID | None
    channel_campaign_step_id: UUID | None
    recipient_id: UUID | None
    dub_folder_id: str | None
    dub_tag_ids: list[str]
    attribution_context: dict[str, Any]
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


_COLS = (
    "id, dub_link_id, dub_external_id, dub_short_url, dub_domain, dub_key, "
    "destination_url, dmaas_design_id, direct_mail_piece_id, brand_id, "
    "channel_campaign_step_id, recipient_id, "
    "dub_folder_id, dub_tag_ids, "
    "attribution_context, created_by_user_id, created_at, updated_at"
)


def _row_to_record(row: tuple) -> DubLinkRecord:
    return DubLinkRecord(
        id=row[0],
        dub_link_id=row[1],
        dub_external_id=row[2],
        dub_short_url=row[3],
        dub_domain=row[4],
        dub_key=row[5],
        destination_url=row[6],
        dmaas_design_id=row[7],
        direct_mail_piece_id=row[8],
        brand_id=row[9],
        channel_campaign_step_id=row[10],
        recipient_id=row[11],
        dub_folder_id=row[12],
        dub_tag_ids=list(row[13] or []),
        attribution_context=row[14] or {},
        created_by_user_id=row[15],
        created_at=row[16],
        updated_at=row[17],
    )


async def insert_dub_link(
    *,
    dub_link_id: str,
    dub_external_id: str | None,
    dub_short_url: str,
    dub_domain: str,
    dub_key: str,
    destination_url: str,
    dmaas_design_id: UUID | None,
    direct_mail_piece_id: UUID | None,
    brand_id: UUID | None,
    attribution_context: dict[str, Any],
    created_by_user_id: UUID | None,
    channel_campaign_step_id: UUID | None = None,
    recipient_id: UUID | None = None,
    dub_folder_id: str | None = None,
    dub_tag_ids: list[str] | None = None,
) -> DubLinkRecord:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dmaas_dub_links "
            f"(dub_link_id, dub_external_id, dub_short_url, dub_domain, dub_key, "
            f"destination_url, dmaas_design_id, direct_mail_piece_id, brand_id, "
            f"channel_campaign_step_id, recipient_id, "
            f"dub_folder_id, dub_tag_ids, "
            f"attribution_context, created_by_user_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_COLS}",
            (
                dub_link_id,
                dub_external_id,
                dub_short_url,
                dub_domain,
                dub_key,
                destination_url,
                str(dmaas_design_id) if dmaas_design_id else None,
                str(direct_mail_piece_id) if direct_mail_piece_id else None,
                str(brand_id) if brand_id else None,
                str(channel_campaign_step_id) if channel_campaign_step_id else None,
                str(recipient_id) if recipient_id else None,
                dub_folder_id,
                Jsonb(list(dub_tag_ids or [])),
                Jsonb(attribution_context or {}),
                str(created_by_user_id) if created_by_user_id else None,
            ),
        )
        row = await cur.fetchone()
    return _row_to_record(row)


async def bulk_insert_dub_links(
    rows: list[dict[str, Any]],
) -> list[DubLinkRecord]:
    """Bulk INSERT into dmaas_dub_links — one multi-VALUES query, ON CONFLICT
    on the (channel_campaign_step_id, recipient_id) partial unique index.

    Returns only the newly inserted rows; previously-existing rows are
    preserved in place (the partial unique index from migration
    20260430T130000 is the conflict target). Caller should re-query the
    post-insert state if it needs the full set.
    """
    if not rows:
        return []

    placeholders = ", ".join(
        ["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"]
        * len(rows)
    )
    flat: list[Any] = []
    for r in rows:
        flat.extend(
            [
                r["dub_link_id"],
                r.get("dub_external_id"),
                r["dub_short_url"],
                r["dub_domain"],
                r["dub_key"],
                r["destination_url"],
                str(r["dmaas_design_id"]) if r.get("dmaas_design_id") else None,
                str(r["direct_mail_piece_id"])
                if r.get("direct_mail_piece_id")
                else None,
                str(r["brand_id"]) if r.get("brand_id") else None,
                str(r["channel_campaign_step_id"])
                if r.get("channel_campaign_step_id")
                else None,
                str(r["recipient_id"]) if r.get("recipient_id") else None,
                r.get("dub_folder_id"),
                Jsonb(list(r.get("dub_tag_ids") or [])),
                Jsonb(r.get("attribution_context") or {}),
                str(r["created_by_user_id"]) if r.get("created_by_user_id") else None,
            ]
        )

    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dmaas_dub_links "
            f"(dub_link_id, dub_external_id, dub_short_url, dub_domain, dub_key, "
            f"destination_url, dmaas_design_id, direct_mail_piece_id, brand_id, "
            f"channel_campaign_step_id, recipient_id, "
            f"dub_folder_id, dub_tag_ids, "
            f"attribution_context, created_by_user_id) "
            f"VALUES {placeholders} "
            f"ON CONFLICT (channel_campaign_step_id, recipient_id) "
            f"WHERE channel_campaign_step_id IS NOT NULL "
            f"AND recipient_id IS NOT NULL DO NOTHING "
            f"RETURNING {_COLS}",
            flat,
        )
        out_rows = await cur.fetchall()
    return [_row_to_record(r) for r in out_rows]


async def get_dub_link_by_dub_id(dub_link_id: str) -> DubLinkRecord | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dmaas_dub_links WHERE dub_link_id = %s",
            (dub_link_id,),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


async def list_dub_links_for_design(design_id: UUID) -> list[DubLinkRecord]:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dmaas_dub_links WHERE dmaas_design_id = %s "
            f"ORDER BY created_at DESC",
            (str(design_id),),
        )
        rows = await cur.fetchall()
    return [_row_to_record(r) for r in rows]


async def list_dub_links_for_step(
    channel_campaign_step_id: UUID,
) -> list[DubLinkRecord]:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dmaas_dub_links "
            f"WHERE channel_campaign_step_id = %s "
            f"ORDER BY created_at",
            (str(channel_campaign_step_id),),
        )
        rows = await cur.fetchall()
    return [_row_to_record(r) for r in rows]


async def find_dub_link_for_step_recipient(
    *,
    channel_campaign_step_id: UUID,
    recipient_id: UUID,
) -> DubLinkRecord | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dmaas_dub_links "
            f"WHERE channel_campaign_step_id = %s AND recipient_id = %s",
            (str(channel_campaign_step_id), str(recipient_id)),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


async def find_dub_link_for_step_short_code(
    *,
    channel_campaign_step_id: UUID,
    short_code: str,
) -> DubLinkRecord | None:
    """Lookup a dmaas_dub_links row by (step_id, dub_key).

    The hosted landing-page render path resolves recipients via
    `(step_id, short_code)` — short_code is the trailing path segment
    of the Dub short URL (e.g. "abc123" in https://track.acme.com/abc123)
    which maps 1:1 to the `dub_key` column.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dmaas_dub_links "
            f"WHERE channel_campaign_step_id = %s AND dub_key = %s",
            (str(channel_campaign_step_id), short_code),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None
