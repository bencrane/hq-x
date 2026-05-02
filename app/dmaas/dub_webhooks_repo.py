"""DB read/write for dub_webhooks — local mirror of webhooks we've registered
in Dub programmatically. Mirrors `app/dmaas/dub_links.py` style.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection


@dataclass
class DubWebhookRecord:
    id: UUID
    dub_webhook_id: str
    name: str
    receiver_url: str
    secret_hash: str | None
    triggers: list[str]
    environment: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


_COLS = (
    "id, dub_webhook_id, name, receiver_url, secret_hash, triggers, "
    "environment, is_active, created_at, updated_at"
)


def _row_to_record(row: tuple) -> DubWebhookRecord:
    return DubWebhookRecord(
        id=row[0],
        dub_webhook_id=row[1],
        name=row[2],
        receiver_url=row[3],
        secret_hash=row[4],
        triggers=list(row[5] or []),
        environment=row[6],
        is_active=row[7],
        created_at=row[8],
        updated_at=row[9],
    )


async def insert_dub_webhook(
    *,
    dub_webhook_id: str,
    name: str,
    receiver_url: str,
    triggers: list[str],
    environment: str,
    secret_hash: str | None = None,
    is_active: bool = True,
) -> DubWebhookRecord:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dub_webhooks "
            f"(dub_webhook_id, name, receiver_url, secret_hash, triggers, "
            f"environment, is_active) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_COLS}",
            (
                dub_webhook_id,
                name,
                receiver_url,
                secret_hash,
                Jsonb(list(triggers or [])),
                environment,
                is_active,
            ),
        )
        row = await cur.fetchone()
    return _row_to_record(row)


async def list_dub_webhooks_for_environment(
    environment: str,
    *,
    only_active: bool = True,
) -> list[DubWebhookRecord]:
    if only_active:
        sql = (
            f"SELECT {_COLS} FROM dub_webhooks "
            f"WHERE environment = %s AND is_active "
            f"ORDER BY created_at DESC"
        )
    else:
        sql = (
            f"SELECT {_COLS} FROM dub_webhooks "
            f"WHERE environment = %s "
            f"ORDER BY created_at DESC"
        )
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, (environment,))
        rows = await cur.fetchall()
    return [_row_to_record(r) for r in rows]


async def get_dub_webhook_by_dub_id(dub_webhook_id: str) -> DubWebhookRecord | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dub_webhooks WHERE dub_webhook_id = %s",
            (dub_webhook_id,),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


async def find_active_for_receiver(
    *,
    environment: str,
    receiver_url: str,
) -> DubWebhookRecord | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM dub_webhooks "
            f"WHERE environment = %s AND receiver_url = %s AND is_active "
            f"ORDER BY created_at DESC LIMIT 1",
            (environment, receiver_url),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


async def deactivate_dub_webhook(dub_webhook_id: str) -> DubWebhookRecord | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE dub_webhooks SET is_active = FALSE, updated_at = NOW() "
            f"WHERE dub_webhook_id = %s "
            f"RETURNING {_COLS}",
            (dub_webhook_id,),
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


async def delete_dub_webhook(dub_webhook_id: str) -> bool:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM dub_webhooks WHERE dub_webhook_id = %s",
            (dub_webhook_id,),
        )
        return cur.rowcount > 0


