"""Recipient resolution service.

A recipient is the channel-agnostic identity for a business / property /
person we contact. This module owns:

  * ``upsert_recipient``      — single-row upsert by natural key
                                ``(organization_id, external_source, external_id)``.
  * ``bulk_upsert_recipients`` — efficient batch upsert that dedupes the
                                 input list before hitting the DB.
  * ``get_recipient`` / ``list_recipients_for_step`` — read helpers.

Recipients are organization-scoped. The same DOT in two organizations is
two distinct rows; the application layer must never resolve a recipient
across orgs.

Natural-key normalization is the caller's responsibility: ``external_id``
is stored as-given. Audience source adapters (FMCSA, NYC RE, manual
upload) are expected to lowercase / strip-pad / canonicalize before
calling here so dedupe is reliable.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.recipients import (
    RecipientResponse,
    RecipientSpec,
    StepRecipientResponse,
)

logger = logging.getLogger(__name__)


_RECIPIENT_COLUMNS = (
    "id, organization_id, recipient_type, external_source, external_id, "
    "display_name, mailing_address, phone, email, metadata, "
    "created_at, updated_at"
)


def _row_to_recipient(row: tuple[Any, ...]) -> RecipientResponse:
    return RecipientResponse(
        id=row[0],
        organization_id=row[1],
        recipient_type=row[2],
        external_source=row[3],
        external_id=row[4],
        display_name=row[5],
        mailing_address=row[6] or {},
        phone=row[7],
        email=row[8],
        metadata=row[9] or {},
        created_at=row[10],
        updated_at=row[11],
    )


async def upsert_recipient(
    *, organization_id: UUID, spec: RecipientSpec
) -> RecipientResponse:
    """Upsert by ``(organization_id, external_source, external_id)``.

    On conflict, mutable attributes (display_name, mailing_address, phone,
    email, recipient_type) are merged with COALESCE preferring the
    incoming value when non-null; metadata is shallow-merged
    (``existing || new``).
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.recipients
                    (organization_id, recipient_type, external_source,
                     external_id, display_name, mailing_address, phone,
                     email, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (organization_id, external_source, external_id)
                DO UPDATE SET
                    recipient_type = EXCLUDED.recipient_type,
                    display_name = COALESCE(
                        EXCLUDED.display_name, business.recipients.display_name
                    ),
                    mailing_address = CASE
                        WHEN EXCLUDED.mailing_address = '{{}}'::jsonb
                          THEN business.recipients.mailing_address
                        ELSE EXCLUDED.mailing_address
                    END,
                    phone = COALESCE(EXCLUDED.phone, business.recipients.phone),
                    email = COALESCE(EXCLUDED.email, business.recipients.email),
                    metadata = business.recipients.metadata || EXCLUDED.metadata,
                    updated_at = NOW()
                RETURNING {_RECIPIENT_COLUMNS}
                """,
                (
                    str(organization_id),
                    spec.recipient_type,
                    spec.external_source,
                    spec.external_id,
                    spec.display_name,
                    Jsonb(spec.mailing_address),
                    spec.phone,
                    spec.email,
                    Jsonb(spec.metadata),
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_recipient(row)


async def bulk_upsert_recipients(
    *, organization_id: UUID, specs: list[RecipientSpec]
) -> list[RecipientResponse]:
    """Bulk variant. Dedupes the input list by natural key before
    inserting, so callers can pass raw audience output without first
    grouping. Order of return matches the deduped input order.
    """
    if not specs:
        return []

    seen: dict[tuple[str, str], RecipientSpec] = {}
    for s in specs:
        seen[(s.external_source, s.external_id)] = s
    deduped = list(seen.values())

    out: list[RecipientResponse] = []
    for spec in deduped:
        out.append(
            await upsert_recipient(organization_id=organization_id, spec=spec)
        )
    return out


async def get_recipient(
    *, recipient_id: UUID, organization_id: UUID
) -> RecipientResponse | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_RECIPIENT_COLUMNS}
                FROM business.recipients
                WHERE id = %s AND organization_id = %s AND deleted_at IS NULL
                """,
                (str(recipient_id), str(organization_id)),
            )
            row = await cur.fetchone()
    return _row_to_recipient(row) if row else None


async def find_recipient_by_natural_key(
    *,
    organization_id: UUID,
    external_source: str,
    external_id: str,
) -> RecipientResponse | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_RECIPIENT_COLUMNS}
                FROM business.recipients
                WHERE organization_id = %s
                  AND external_source = %s
                  AND external_id = %s
                  AND deleted_at IS NULL
                """,
                (str(organization_id), external_source, external_id),
            )
            row = await cur.fetchone()
    return _row_to_recipient(row) if row else None


# ── Step memberships ─────────────────────────────────────────────────────


_MEMBERSHIP_COLUMNS = (
    "id, channel_campaign_step_id, recipient_id, organization_id, status, "
    "scheduled_for, processed_at, error_reason, metadata, "
    "created_at, updated_at"
)


def _row_to_membership(row: tuple[Any, ...]) -> StepRecipientResponse:
    return StepRecipientResponse(
        id=row[0],
        channel_campaign_step_id=row[1],
        recipient_id=row[2],
        organization_id=row[3],
        status=row[4],
        scheduled_for=row[5],
        processed_at=row[6],
        error_reason=row[7],
        metadata=row[8] or {},
        created_at=row[9],
        updated_at=row[10],
    )


async def list_step_memberships(
    *,
    channel_campaign_step_id: UUID,
    status: str | None = None,
) -> list[StepRecipientResponse]:
    where = ["channel_campaign_step_id = %s"]
    args: list[Any] = [str(channel_campaign_step_id)]
    if status is not None:
        where.append("status = %s")
        args.append(status)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_MEMBERSHIP_COLUMNS}
                FROM business.channel_campaign_step_recipients
                WHERE {' AND '.join(where)}
                ORDER BY created_at
                """,
                args,
            )
            rows = await cur.fetchall()
    return [_row_to_membership(r) for r in rows]


async def find_membership_for_recipient(
    *,
    channel_campaign_step_id: UUID,
    recipient_id: UUID,
) -> StepRecipientResponse | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_MEMBERSHIP_COLUMNS}
                FROM business.channel_campaign_step_recipients
                WHERE channel_campaign_step_id = %s AND recipient_id = %s
                """,
                (str(channel_campaign_step_id), str(recipient_id)),
            )
            row = await cur.fetchone()
    return _row_to_membership(row) if row else None


async def update_membership_status(
    *,
    membership_id: UUID,
    new_status: str,
    error_reason: str | None = None,
    set_processed_at: bool = False,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaign_step_recipients
                SET status = %s,
                    error_reason = COALESCE(%s, error_reason),
                    processed_at = CASE
                        WHEN %s THEN COALESCE(processed_at, NOW())
                        ELSE processed_at
                    END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (new_status, error_reason, set_processed_at, str(membership_id)),
            )


async def bulk_update_pending_to_scheduled(
    *,
    channel_campaign_step_id: UUID,
) -> int:
    """Activation helper: flip every ``pending`` row for a step to
    ``scheduled``. Returns the number of rows flipped.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_step_recipients
                SET status = 'scheduled', updated_at = NOW()
                WHERE channel_campaign_step_id = %s AND status = 'pending'
                """,
                (str(channel_campaign_step_id),),
            )
            return cur.rowcount or 0


__all__ = [
    "upsert_recipient",
    "bulk_upsert_recipients",
    "get_recipient",
    "find_recipient_by_natural_key",
    "list_step_memberships",
    "find_membership_for_recipient",
    "update_membership_status",
    "bulk_update_pending_to_scheduled",
]
