"""GTM signal definition registry (hq-x).

Migrated from DEX ``ops.gtm_signals``. hq-x owns the signal definition +
lifecycle; warehouse SQL compute runs in DEX (``/api/internal/signals/compute``)
and the gtm MCP. This module is config-only persistence over
``business.gtm_signals`` — no DuckDB/Lance/R2.

Mirrors ``recipients.py``: async ``get_db_connection()`` + ``Jsonb`` for jsonb
params. Returns plain dicts (no Pydantic response model yet).
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)

_SIGNAL_COLUMNS = (
    "signal_slug, display_name, spine_target, criteria, "
    "webhook_test_url, webhook_prod_url, webhook_target, is_active, "
    "created_at, updated_at"
)

# Fields a PATCH may set (criteria is jsonb-wrapped at write time).
_PATCHABLE = {
    "display_name",
    "spine_target",
    "criteria",
    "webhook_test_url",
    "webhook_prod_url",
    "webhook_target",
    "is_active",
}


def _row_to_signal(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "signal_slug": row[0],
        "display_name": row[1] or row[0],
        "spine_target": row[2],
        "criteria": row[3] or {},
        "webhook_test_url": row[4] or "",
        "webhook_prod_url": row[5] or "",
        "webhook_target": row[6],
        "is_active": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
        "updated_at": row[9].isoformat() if row[9] else None,
    }


async def list_signals() -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM business.gtm_signals "
                "ORDER BY is_active DESC, signal_slug ASC"
            )
            rows = await cur.fetchall()
    return [_row_to_signal(r) for r in rows]


async def list_active_signals() -> list[dict[str, Any]]:
    """Active signals only — the cron's working set."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM business.gtm_signals "
                "WHERE is_active ORDER BY signal_slug ASC"
            )
            rows = await cur.fetchall()
    return [_row_to_signal(r) for r in rows]


async def get_signal(signal_slug: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM business.gtm_signals "
                "WHERE signal_slug = %s",
                (signal_slug,),
            )
            row = await cur.fetchone()
    return _row_to_signal(row) if row else None


async def upsert_signal(spec: dict[str, Any]) -> dict[str, Any]:
    """Insert-or-update by ``signal_slug``. Used by the backfill + authoring."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.gtm_signals
                    (signal_slug, display_name, spine_target, criteria,
                     webhook_test_url, webhook_prod_url, webhook_target, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_slug) DO UPDATE SET
                    display_name     = EXCLUDED.display_name,
                    spine_target     = EXCLUDED.spine_target,
                    criteria         = EXCLUDED.criteria,
                    webhook_test_url = EXCLUDED.webhook_test_url,
                    webhook_prod_url = EXCLUDED.webhook_prod_url,
                    webhook_target   = EXCLUDED.webhook_target,
                    is_active        = EXCLUDED.is_active,
                    updated_at       = NOW()
                RETURNING {_SIGNAL_COLUMNS}
                """,
                (
                    spec["signal_slug"],
                    spec.get("display_name") or "",
                    spec["spine_target"],
                    Jsonb(spec["criteria"]),
                    spec.get("webhook_test_url") or "",
                    spec.get("webhook_prod_url") or "",
                    spec.get("webhook_target") or "test",
                    bool(spec.get("is_active", True)),
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_signal(row)


async def patch_signal(
    signal_slug: str, patch: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update. Unknown keys are ignored; empty patch is a no-op read."""
    fields = {k: v for k, v in patch.items() if k in _PATCHABLE}
    if not fields:
        return await get_signal(signal_slug)
    sets: list[str] = []
    args: list[Any] = []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        args.append(Jsonb(value) if key == "criteria" else value)
    args.append(signal_slug)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.gtm_signals
                SET {', '.join(sets)}, updated_at = NOW()
                WHERE signal_slug = %s
                RETURNING {_SIGNAL_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
    return _row_to_signal(row) if row else None


async def delete_signal(signal_slug: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM business.gtm_signals WHERE signal_slug = %s",
                (signal_slug,),
            )
            return (cur.rowcount or 0) > 0


__all__ = [
    "list_signals",
    "list_active_signals",
    "get_signal",
    "upsert_signal",
    "patch_signal",
    "delete_signal",
]
