"""DB read/write for business.entri_domain_connections.

The table tracks one row per customer-owned hostname that we serve through
Entri's reverse proxy. Webhook payloads are persisted in the existing
`webhook_events` table (provider_slug='entri'); this repository only owns
the projection / state-machine read side.

Mirrors `app/dmaas/dub_links.py` for style.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


@dataclass
class EntriDomainConnection:
    id: UUID
    organization_id: UUID
    channel_campaign_step_id: UUID | None
    domain: str
    is_root_domain: bool
    application_url: str
    state: str
    entri_user_id: str
    entri_token: str | None
    entri_token_expires_at: datetime | None
    provider: str | None
    setup_type: str | None
    propagation_status: str | None
    power_status: str | None
    secure_status: str | None
    last_webhook_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


_TABLE = "business.entri_domain_connections"
_COLS = (
    "id, organization_id, channel_campaign_step_id, domain, is_root_domain, "
    "application_url, state, entri_user_id, entri_token, entri_token_expires_at, "
    "provider, setup_type, propagation_status, power_status, secure_status, "
    "last_webhook_id, last_error, created_at, updated_at"
)


def _row(row: tuple) -> EntriDomainConnection:
    return EntriDomainConnection(
        id=row[0],
        organization_id=row[1],
        channel_campaign_step_id=row[2],
        domain=row[3],
        is_root_domain=row[4],
        application_url=row[5],
        state=row[6],
        entri_user_id=row[7],
        entri_token=row[8],
        entri_token_expires_at=row[9],
        provider=row[10],
        setup_type=row[11],
        propagation_status=row[12],
        power_status=row[13],
        secure_status=row[14],
        last_webhook_id=row[15],
        last_error=row[16],
        created_at=row[17],
        updated_at=row[18],
    )


async def insert_pending_connection(
    *,
    organization_id: UUID,
    channel_campaign_step_id: UUID | None,
    domain: str,
    is_root_domain: bool,
    application_url: str,
    entri_user_id: str,
    entri_token: str | None,
    entri_token_expires_at: datetime | None,
) -> EntriDomainConnection:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO {_TABLE} "
            f"(organization_id, channel_campaign_step_id, domain, is_root_domain, "
            f" application_url, state, entri_user_id, entri_token, entri_token_expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, 'pending_modal', %s, %s, %s) "
            f"RETURNING {_COLS}",
            (
                str(organization_id),
                str(channel_campaign_step_id) if channel_campaign_step_id else None,
                domain,
                is_root_domain,
                application_url,
                entri_user_id,
                entri_token,
                entri_token_expires_at,
            ),
        )
        row = await cur.fetchone()
    return _row(row)


async def get_by_id(connection_id: UUID) -> EntriDomainConnection | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM {_TABLE} WHERE id = %s",
            (str(connection_id),),
        )
        row = await cur.fetchone()
    return _row(row) if row else None


async def get_by_domain(domain: str) -> EntriDomainConnection | None:
    """Most-recent row for a given hostname (any state).

    Used by the origin app to resolve `x-entri-forwarded-host` to a
    campaign — only `live` rows are routable, but we return any so callers
    can produce useful 404 reasons.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM {_TABLE} WHERE domain = %s "
            f"ORDER BY created_at DESC LIMIT 1",
            (domain,),
        )
        row = await cur.fetchone()
    return _row(row) if row else None


async def get_by_entri_user_id(entri_user_id: str) -> EntriDomainConnection | None:
    """Find the active connection that minted a session under this userId.

    Used by the webhook projector to correlate inbound events to a row.
    Multiple rows can share an entri_user_id over time (e.g. retries after
    failure), so we pick the most recent non-disconnected row.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM {_TABLE} "
            f"WHERE entri_user_id = %s AND state <> 'disconnected' "
            f"ORDER BY created_at DESC LIMIT 1",
            (entri_user_id,),
        )
        row = await cur.fetchone()
    return _row(row) if row else None


async def list_for_organization(organization_id: UUID) -> list[EntriDomainConnection]:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLS} FROM {_TABLE} WHERE organization_id = %s "
            f"ORDER BY created_at DESC",
            (str(organization_id),),
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows]


async def update_state(
    connection_id: UUID,
    *,
    state: str | None = None,
    provider: str | None = None,
    setup_type: str | None = None,
    propagation_status: str | None = None,
    power_status: str | None = None,
    secure_status: str | None = None,
    last_webhook_id: str | None = None,
    last_error: str | None = None,
) -> EntriDomainConnection | None:
    """Patch a subset of fields. Any field passed as None is left unchanged."""
    fields: list[str] = []
    values: list[Any] = []

    def _set(name: str, value: Any) -> None:
        if value is None:
            return
        fields.append(f"{name} = %s")
        values.append(value)

    _set("state", state)
    _set("provider", provider)
    _set("setup_type", setup_type)
    _set("propagation_status", propagation_status)
    _set("power_status", power_status)
    _set("secure_status", secure_status)
    _set("last_webhook_id", last_webhook_id)
    _set("last_error", last_error)
    if not fields:
        return await get_by_id(connection_id)

    values.append(str(connection_id))
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE {_TABLE} SET {', '.join(fields)} "
            f"WHERE id = %s RETURNING {_COLS}",
            tuple(values),
        )
        row = await cur.fetchone()
    return _row(row) if row else None


async def mark_disconnected(connection_id: UUID) -> EntriDomainConnection | None:
    return await update_state(connection_id, state="disconnected")
