"""CRUD + lifecycle helpers for business.gtm_initiatives.

Mirrors exa_research_jobs.py / activation_jobs.py — Postgres is the
source of truth for initiative state, and async subagents (strategic
context researcher, strategy synthesizer) drive transitions through
the public router endpoints.

State machine for slice 1:

    draft
      └─→ awaiting_strategic_research
            └─→ strategic_research_ready
                  └─→ awaiting_strategy_synthesis
                        ├─→ strategy_ready
                        └─→ failed

The downstream states (`materializing`, `ready_to_launch`, `active`,
`completed`, `cancelled`) are present in the DB enum so future
directives don't have to migrate the check constraint.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


class GtmInitiativeError(Exception):
    pass


class GtmInitiativeNotFound(GtmInitiativeError):
    pass


class InvalidStatusTransition(GtmInitiativeError):
    pass


# Allowed transitions for slice 1. Listed explicitly so a stray status
# string can't sneak past the state-machine guard.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"awaiting_strategic_research", "cancelled"},
    "awaiting_strategic_research": {"strategic_research_ready", "failed", "cancelled"},
    "strategic_research_ready": {"awaiting_strategy_synthesis", "cancelled"},
    "awaiting_strategy_synthesis": {"strategy_ready", "failed", "cancelled"},
    "strategy_ready": {"materializing", "cancelled"},
    "failed": {"awaiting_strategic_research", "awaiting_strategy_synthesis", "cancelled"},
    "materializing": {"ready_to_launch", "failed", "cancelled"},
    "ready_to_launch": {"active", "cancelled"},
    "active": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


_COLUMNS = (
    "id, organization_id, brand_id, partner_id, partner_contract_id, "
    "data_engine_audience_id, partner_research_ref, "
    "strategic_context_research_ref, campaign_strategy_path, "
    "status, history, metadata, reservation_window_start, "
    "reservation_window_end, created_at, updated_at"
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "organization_id": row[1],
        "brand_id": row[2],
        "partner_id": row[3],
        "partner_contract_id": row[4],
        "data_engine_audience_id": row[5],
        "partner_research_ref": row[6],
        "strategic_context_research_ref": row[7],
        "campaign_strategy_path": row[8],
        "status": row[9],
        "history": row[10] or [],
        "metadata": row[11] or {},
        "reservation_window_start": row[12],
        "reservation_window_end": row[13],
        "created_at": row[14],
        "updated_at": row[15],
    }


async def create_initiative(
    *,
    organization_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
    partner_research_ref: str | None = None,
    reservation_window_start: datetime | None = None,
    reservation_window_end: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a new gtm_initiatives row in status='draft'."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.gtm_initiatives (
                    organization_id, brand_id, partner_id, partner_contract_id,
                    data_engine_audience_id, partner_research_ref,
                    reservation_window_start, reservation_window_end,
                    metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    str(partner_id),
                    str(partner_contract_id),
                    str(data_engine_audience_id),
                    partner_research_ref,
                    reservation_window_start,
                    reservation_window_end,
                    Jsonb(metadata or {}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return _row_to_dict(row)


async def get_initiative(
    initiative_id: UUID,
    *,
    organization_id: UUID | None = None,
) -> dict[str, Any] | None:
    where = ["id = %s"]
    args: list[Any] = [str(initiative_id)]
    if organization_id is not None:
        where.append("organization_id = %s")
        args.append(str(organization_id))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM business.gtm_initiatives "
                f"WHERE {' AND '.join(where)}",
                args,
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def list_initiatives_for_org(
    organization_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.gtm_initiatives
                WHERE organization_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (str(organization_id), limit, offset),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def transition_status(
    initiative_id: UUID,
    *,
    new_status: str,
    history_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move the initiative to ``new_status`` if the transition is allowed.

    Refuses illegal transitions with ``InvalidStatusTransition`` so a
    misordered subagent call surfaces clearly instead of corrupting the
    state machine.
    """
    current = await get_initiative(initiative_id)
    if current is None:
        raise GtmInitiativeNotFound(f"initiative {initiative_id} not found")
    allowed = _ALLOWED_TRANSITIONS.get(current["status"], set())
    if new_status not in allowed:
        raise InvalidStatusTransition(
            f"cannot transition initiative {initiative_id} from "
            f"{current['status']!r} to {new_status!r}"
        )
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "kind": "transition",
        "from_status": current["status"],
        "to_status": new_status,
    }
    if history_event:
        entry.update(history_event)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.gtm_initiatives
                SET status = %s,
                    history = history || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING {_COLUMNS}
                """,
                (new_status, json.dumps([entry]), str(initiative_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return _row_to_dict(row)


async def append_history(
    initiative_id: UUID,
    event: dict[str, Any],
) -> None:
    """Best-effort history append. Never raises into the caller."""
    entry = {"at": datetime.now(UTC).isoformat(), **event}
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE business.gtm_initiatives
                    SET history = history || %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (json.dumps([entry]), str(initiative_id)),
                )
            await conn.commit()
    except Exception:  # pragma: no cover — observability
        logger.exception(
            "gtm_initiatives.append_history failed",
            extra={"initiative_id": str(initiative_id)},
        )


async def set_strategic_context_research_ref(
    initiative_id: UUID, ref: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET strategic_context_research_ref = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (ref, str(initiative_id)),
            )
        await conn.commit()


async def set_campaign_strategy_path(
    initiative_id: UUID, path: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET campaign_strategy_path = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (path, str(initiative_id)),
            )
        await conn.commit()


__all__ = [
    "GtmInitiativeError",
    "GtmInitiativeNotFound",
    "InvalidStatusTransition",
    "create_initiative",
    "get_initiative",
    "list_initiatives_for_org",
    "transition_status",
    "append_history",
    "set_strategic_context_research_ref",
    "set_campaign_strategy_path",
]
