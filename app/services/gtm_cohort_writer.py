"""Persist resolved signal cohorts (hq-x).

A cohort is the materialized result of a signal's criteria against the
warehouse (computed in DEX / the gtm MCP). Stored as a header row
(``business.gtm_signal_cohorts``, one per run) plus N member rows
(``business.gtm_signal_cohort_members``, each a dataset-agnostic jsonb).
Reusable with zero recompute; ``criteria_snapshot`` keeps it re-runnable.

Mirrors ``recipients.py``: async ``get_db_connection()`` + ``Jsonb``.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


async def write_cohort(
    *,
    signal_slug: str,
    criteria_snapshot: dict[str, Any],
    spine_target: str,
    matched_count: int,
    members: list[dict[str, Any]],
    truncated: bool,
    source: str,
    compute_ms: int | None = None,
    trigger_run_id: str | None = None,
    dispatch: dict[str, Any] | None = None,
) -> UUID:
    """Insert one cohort header + its members in a single transaction.

    ``member_count`` is derived from ``len(members)``; ``matched_count`` is the
    true pre-cap total (DEX-reported). ``ordinal`` preserves the order_by sort.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO business.gtm_signal_cohorts
                        (signal_slug, criteria_snapshot, spine_target,
                         matched_count, member_count, truncated, source,
                         compute_ms, trigger_run_id, dispatch)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        signal_slug,
                        Jsonb(criteria_snapshot),
                        spine_target,
                        matched_count,
                        len(members),
                        truncated,
                        source,
                        compute_ms,
                        trigger_run_id,
                        Jsonb(dispatch) if dispatch is not None else None,
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                cohort_id: UUID = row[0]
                if members:
                    await cur.executemany(
                        """
                        INSERT INTO business.gtm_signal_cohort_members
                            (cohort_id, ordinal, member)
                        VALUES (%s, %s, %s)
                        """,
                        [
                            (str(cohort_id), i, Jsonb(member))
                            for i, member in enumerate(members)
                        ],
                    )
    logger.info(
        "gtm_cohort_writer.write_cohort slug=%s cohort_id=%s members=%d "
        "matched=%d truncated=%s source=%s",
        signal_slug, cohort_id, len(members), matched_count, truncated, source,
    )
    return cohort_id


async def get_cohort(cohort_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, signal_slug, run_at, criteria_snapshot, spine_target,
                       matched_count, member_count, truncated, source,
                       compute_ms, trigger_run_id, dispatch
                FROM business.gtm_signal_cohorts
                WHERE id = %s
                """,
                (str(cohort_id),),
            )
            header = await cur.fetchone()
            if header is None:
                return None
            await cur.execute(
                """
                SELECT member FROM business.gtm_signal_cohort_members
                WHERE cohort_id = %s ORDER BY ordinal
                """,
                (str(cohort_id),),
            )
            members = await cur.fetchall()
    return {
        "id": str(header[0]),
        "signal_slug": header[1],
        "run_at": header[2].isoformat() if header[2] else None,
        "criteria_snapshot": header[3] or {},
        "spine_target": header[4],
        "matched_count": header[5],
        "member_count": header[6],
        "truncated": header[7],
        "source": header[8],
        "compute_ms": header[9],
        "trigger_run_id": header[10],
        "dispatch": header[11],
        "members": [m[0] for m in members],
    }


async def list_cohorts_for_signal(
    signal_slug: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, signal_slug, run_at, matched_count, member_count,
                       truncated, source, compute_ms
                FROM business.gtm_signal_cohorts
                WHERE signal_slug = %s
                ORDER BY run_at DESC
                LIMIT %s OFFSET %s
                """,
                (signal_slug, limit, offset),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "signal_slug": r[1],
            "run_at": r[2].isoformat() if r[2] else None,
            "matched_count": r[3],
            "member_count": r[4],
            "truncated": r[5],
            "source": r[6],
            "compute_ms": r[7],
        }
        for r in rows
    ]


__all__ = ["write_cohort", "get_cohort", "list_cohorts_for_signal"]
