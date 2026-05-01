"""CRUD + lookup helpers for business.initiative_recipient_memberships.

The manifest of "what was paid for" per initiative. Populated by the
audience-materializer subagent at initiative materialization time;
read by voice-agent inbound routing, billing reconciliation, and the
materializer's own overlap-detection logic.

No router exposes this module today — callers are internal services
only. The schema-level partial unique index
``uq_irm_active_recipient_per_initiative`` enforces that a recipient
can hold at most one active membership per initiative; a soft-deleted
row (``removed_at IS NOT NULL``) does not block a new active insert,
so re-adding after removal works.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db import get_db_connection


_COLUMNS = (
    "id, initiative_id, partner_contract_id, recipient_id, "
    "data_engine_audience_id, first_seen_channel_campaign_id, "
    "added_at, removed_at, removed_reason"
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "initiative_id": row[1],
        "partner_contract_id": row[2],
        "recipient_id": row[3],
        "data_engine_audience_id": row[4],
        "first_seen_channel_campaign_id": row[5],
        "added_at": row[6],
        "removed_at": row[7],
        "removed_reason": row[8],
    }


async def add_membership(
    *,
    initiative_id: UUID,
    partner_contract_id: UUID,
    recipient_id: UUID,
    data_engine_audience_id: UUID,
    first_seen_channel_campaign_id: UUID | None = None,
) -> dict[str, Any]:
    """Insert a new active membership, or return the existing active row
    if one already exists for this (initiative, recipient) pair.

    The partial unique index drives ON CONFLICT; we do nothing on
    conflict and re-fetch the active row so the caller gets a
    consistent shape regardless of whether it was newly inserted.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.initiative_recipient_memberships
                    (initiative_id, partner_contract_id, recipient_id,
                     data_engine_audience_id, first_seen_channel_campaign_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (initiative_id, recipient_id)
                    WHERE removed_at IS NULL
                DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (
                    str(initiative_id),
                    str(partner_contract_id),
                    str(recipient_id),
                    str(data_engine_audience_id),
                    str(first_seen_channel_campaign_id)
                    if first_seen_channel_campaign_id
                    else None,
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return _row_to_dict(row)
            # Pre-existing active row: re-fetch and return it.
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.initiative_recipient_memberships
                WHERE initiative_id = %s
                  AND recipient_id = %s
                  AND removed_at IS NULL
                """,
                (str(initiative_id), str(recipient_id)),
            )
            existing = await cur.fetchone()
    assert existing is not None  # ON CONFLICT path implies the row exists
    return _row_to_dict(existing)


async def remove_membership(
    *,
    initiative_id: UUID,
    recipient_id: UUID,
    reason: str,
) -> None:
    """Soft-delete the active membership for this (initiative, recipient).

    No-op if no active membership exists.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.initiative_recipient_memberships
                SET removed_at = NOW(),
                    removed_reason = %s
                WHERE initiative_id = %s
                  AND recipient_id = %s
                  AND removed_at IS NULL
                """,
                (reason, str(initiative_id), str(recipient_id)),
            )


async def find_active_for_recipient(
    recipient_id: UUID,
) -> list[dict[str, Any]]:
    """Return all active memberships for this recipient.

    In theory this is 0 or 1, but the schema permits multiple if a
    recipient is in two non-overlapping-window initiatives at once;
    callers (e.g. voice-agent inbound resolution) filter as needed.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.initiative_recipient_memberships
                WHERE recipient_id = %s
                  AND removed_at IS NULL
                ORDER BY added_at DESC
                """,
                (str(recipient_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def find_active_by_audience_spec(
    data_engine_audience_id: UUID,
) -> list[dict[str, Any]]:
    """Active memberships sharing a frozen audience spec.

    Used by the materializer to detect "this recipient already paid-for
    under a different initiative with the same spec" conflicts.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.initiative_recipient_memberships
                WHERE data_engine_audience_id = %s
                  AND removed_at IS NULL
                ORDER BY added_at DESC
                """,
                (str(data_engine_audience_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_active_for_contract(
    *,
    partner_contract_id: UUID,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Billing reconciliation + contract-fulfillment reporting."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.initiative_recipient_memberships
                WHERE partner_contract_id = %s
                  AND removed_at IS NULL
                ORDER BY added_at DESC
                LIMIT %s OFFSET %s
                """,
                (str(partner_contract_id), limit, offset),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def count_active_for_initiative(initiative_id: UUID) -> int:
    """Quick health metric for an in-flight initiative."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM business.initiative_recipient_memberships
                WHERE initiative_id = %s
                  AND removed_at IS NULL
                """,
                (str(initiative_id),),
            )
            row = await cur.fetchone()
    assert row is not None
    return int(row[0])


__all__ = [
    "add_membership",
    "remove_membership",
    "find_active_for_recipient",
    "find_active_by_audience_spec",
    "list_active_for_contract",
    "count_active_for_initiative",
]
