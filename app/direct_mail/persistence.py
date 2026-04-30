"""Persistence helpers for direct_mail_pieces + direct_mail_piece_events.

`upsert_piece` is keyed on `(provider_slug, external_piece_id)` — single
provider in this port, but the column stays so a future provider plugs in
without a schema change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


def project_cost_cents(provider_piece: dict[str, Any]) -> int | None:
    """Project Lob's `price` (string dollars, e.g. "0.84") to integer cents.

    Returns None if absent or unparseable.
    """
    raw = provider_piece.get("price")
    if raw is None:
        return None
    try:
        dollars = float(raw)
    except (TypeError, ValueError):
        return None
    return round(dollars * 100)


@dataclass(frozen=True)
class UpsertedPiece:
    id: UUID
    external_piece_id: str
    piece_type: str
    status: str
    cost_cents: int | None
    deliverability: str | None
    is_test_mode: bool
    created_at: datetime
    updated_at: datetime
    raw_payload: dict[str, Any]
    metadata: dict[str, Any] | None
    # Campaign-hierarchy tagging (post-0021/0023). Populated once the Lob
    # send path goes through channel_campaign_steps. NULL for legacy rows.
    channel_campaign_step_id: UUID | None = None
    channel_campaign_id: UUID | None = None
    campaign_id: UUID | None = None


def _row_to_piece(row: tuple[Any, ...]) -> UpsertedPiece:
    return UpsertedPiece(
        id=row[0],
        external_piece_id=row[1],
        piece_type=row[2],
        status=row[3],
        cost_cents=row[4],
        deliverability=row[5],
        is_test_mode=bool(row[6]),
        created_at=row[7],
        updated_at=row[8],
        raw_payload=row[9] or {},
        metadata=row[10],
        channel_campaign_step_id=row[11] if len(row) > 11 else None,
        channel_campaign_id=row[12] if len(row) > 12 else None,
        campaign_id=row[13] if len(row) > 13 else None,
    )


async def upsert_piece(
    *,
    piece_type: str,
    provider_piece: dict[str, Any],
    deliverability: str | None,
    created_by_user_id: UUID | None,
    is_test_mode: bool = False,
    metadata: dict[str, Any] | None = None,
    provider_slug: str = "lob",
    channel_campaign_step_id: UUID | None = None,
    channel_campaign_id: UUID | None = None,
    campaign_id: UUID | None = None,
) -> UpsertedPiece:
    external_piece_id = provider_piece.get("id")
    if not external_piece_id:
        raise ValueError("provider piece is missing an id")
    cost_cents = project_cost_cents(provider_piece)
    send_date = provider_piece.get("send_date")
    status = (
        provider_piece.get("status") or provider_piece.get("expected_delivery_status") or "unknown"
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO direct_mail_pieces
                    (provider_slug, external_piece_id, piece_type, status,
                     send_date, cost_cents, deliverability, is_test_mode,
                     metadata, raw_payload, created_by_user_id,
                     channel_campaign_step_id, channel_campaign_id, campaign_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider_slug, external_piece_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    send_date = COALESCE(EXCLUDED.send_date, direct_mail_pieces.send_date),
                    cost_cents = COALESCE(EXCLUDED.cost_cents, direct_mail_pieces.cost_cents),
                    deliverability = COALESCE(
                        EXCLUDED.deliverability, direct_mail_pieces.deliverability
                    ),
                    metadata = COALESCE(EXCLUDED.metadata, direct_mail_pieces.metadata),
                    raw_payload = EXCLUDED.raw_payload,
                    -- Pre-existing pieces keep their campaign tagging; only stamp
                    -- the columns when the upsert is filling in a NULL.
                    channel_campaign_step_id = COALESCE(
                        direct_mail_pieces.channel_campaign_step_id,
                        EXCLUDED.channel_campaign_step_id
                    ),
                    channel_campaign_id = COALESCE(
                        direct_mail_pieces.channel_campaign_id,
                        EXCLUDED.channel_campaign_id
                    ),
                    campaign_id = COALESCE(
                        direct_mail_pieces.campaign_id, EXCLUDED.campaign_id
                    ),
                    updated_at = NOW()
                RETURNING id, external_piece_id, piece_type, status,
                          cost_cents, deliverability, is_test_mode,
                          created_at, updated_at, raw_payload, metadata,
                          channel_campaign_step_id, channel_campaign_id, campaign_id
                """,
                (
                    provider_slug,
                    external_piece_id,
                    piece_type,
                    status,
                    send_date,
                    cost_cents,
                    deliverability,
                    is_test_mode,
                    Jsonb(metadata) if metadata is not None else None,
                    Jsonb(provider_piece),
                    str(created_by_user_id) if created_by_user_id else None,
                    str(channel_campaign_step_id) if channel_campaign_step_id else None,
                    str(channel_campaign_id) if channel_campaign_id else None,
                    str(campaign_id) if campaign_id else None,
                ),
            )
            row = await cur.fetchone()
    if row is None:
        raise RuntimeError("upsert_piece returned no row")
    return _row_to_piece(row)


async def get_piece_by_external_id(
    *,
    external_piece_id: str,
    piece_type: str | None = None,
    provider_slug: str = "lob",
) -> UpsertedPiece | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if piece_type is None:
                await cur.execute(
                    """
                    SELECT id, external_piece_id, piece_type, status,
                           cost_cents, deliverability, is_test_mode,
                           created_at, updated_at, raw_payload, metadata,
                           channel_campaign_step_id, channel_campaign_id, campaign_id
                    FROM direct_mail_pieces
                    WHERE provider_slug = %s AND external_piece_id = %s
                      AND deleted_at IS NULL
                    """,
                    (provider_slug, external_piece_id),
                )
            else:
                await cur.execute(
                    """
                    SELECT id, external_piece_id, piece_type, status,
                           cost_cents, deliverability, is_test_mode,
                           created_at, updated_at, raw_payload, metadata,
                           channel_campaign_step_id, channel_campaign_id, campaign_id
                    FROM direct_mail_pieces
                    WHERE provider_slug = %s AND external_piece_id = %s
                      AND piece_type = %s AND deleted_at IS NULL
                    """,
                    (provider_slug, external_piece_id, piece_type),
                )
            row = await cur.fetchone()
    return _row_to_piece(row) if row else None


async def append_piece_event(
    *,
    piece_id: UUID,
    event_type: str,
    previous_status: str | None,
    new_status: str | None,
    occurred_at: datetime | None,
    source_event_id: str | None,
    raw_payload: dict[str, Any] | None,
) -> UUID:
    occurred = occurred_at or datetime.now(UTC)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO direct_mail_piece_events
                    (piece_id, event_type, previous_status, new_status,
                     occurred_at, source_event_id, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(piece_id),
                    event_type,
                    previous_status,
                    new_status,
                    occurred,
                    source_event_id,
                    Jsonb(raw_payload) if raw_payload is not None else None,
                ),
            )
            row = await cur.fetchone()
    return row[0]


async def update_piece_status(
    *,
    piece_id: UUID,
    new_status: str,
) -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE direct_mail_pieces
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING status
                """,
                (new_status, str(piece_id)),
            )
            row = await cur.fetchone()
    return row[0] if row else None
