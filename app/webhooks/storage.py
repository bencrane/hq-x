from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.db import get_db_connection


class DuplicateEventError(Exception):
    """Raised when an EmailBison event with the same (provider_slug, event_key) already exists."""


async def insert_cal_raw_event(*, fields: dict[str, Any], payload: dict[str, Any]) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO cal_raw_events
                    (trigger_event, payload, cal_event_uid, organizer_email,
                     attendee_emails, event_type_id, processed)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    fields["trigger_event"],
                    Jsonb(payload),
                    fields["cal_event_uid"],
                    fields["organizer_email"],
                    Jsonb(fields["attendee_emails"]),
                    fields["event_type_id"],
                    False,
                ),
            )
            row = await cur.fetchone()
            return row[0]


async def insert_emailbison_event(
    *,
    event_key: str,
    event_type: str | None,
    payload: dict[str, Any],
) -> UUID:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO webhook_events
                        (provider_slug, event_key, event_type, status,
                         replay_count, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        "emailbison",
                        event_key,
                        event_type,
                        "accepted",
                        0,
                        Jsonb(payload),
                    ),
                )
                row = await cur.fetchone()
                return row[0]
    except UniqueViolation as exc:
        raise DuplicateEventError(event_key) from exc


async def update_webhook_event_status(
    *, event_id: UUID, status: str
) -> None:
    """Update the webhook_events row's status field.

    Status ∈ {'accepted','processed','orphaned','dead_letter'}.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE webhook_events
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, str(event_id)),
            )
