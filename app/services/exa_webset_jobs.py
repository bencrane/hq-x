"""CRUD + lifecycle helpers for business.exa_webset_jobs.

Mirrors the exa_research_jobs service pattern: the Postgres row is the
source of truth; Trigger.dev tasks call back into hq-x's internal
endpoints to drive state transitions.

Idempotency-Key contract: when ``create_job`` is called twice with the
same ``(organization_id, idempotency_key)`` for the same org, the second
call returns the existing job's row without spawning a duplicate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


class ExaWebsetJobError(Exception):
    pass


class ExaWebsetJobNotFound(ExaWebsetJobError):
    pass


_COLUMNS = (
    "id, organization_id, created_by_user_id, dex_run_id, "
    "description, count, criteria, enrichments, entity, "
    "status, exa_webset_id, result_summary, error, history, "
    "trigger_run_id, idempotency_key, attempts, "
    "created_at, started_at, completed_at"
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "organization_id": row[1],
        "created_by_user_id": row[2],
        "dex_run_id": row[3],
        "description": row[4],
        "count": row[5],
        "criteria": row[6] or [],
        "enrichments": row[7],
        "entity": row[8],
        "status": row[9],
        "exa_webset_id": row[10],
        "result_summary": row[11],
        "error": row[12],
        "history": row[13] or [],
        "trigger_run_id": row[14],
        "idempotency_key": row[15],
        "attempts": row[16] or 0,
        "created_at": row[17],
        "started_at": row[18],
        "completed_at": row[19],
    }


async def find_by_idempotency_key(
    *,
    organization_id: UUID,
    idempotency_key: str,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.exa_webset_jobs
                WHERE organization_id = %s AND idempotency_key = %s
                """,
                (str(organization_id), idempotency_key),
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def create_job(
    *,
    organization_id: UUID,
    created_by_user_id: UUID | None,
    description: str,
    count: int,
    criteria: list[dict[str, Any]],
    enrichments: list[dict[str, Any]] | None,
    entity: str = "company",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Insert a new exa_webset_jobs row in status='queued'.

    A new dex_run_id UUID is minted here and will be passed to DEX when
    the webset is created (as Exa's externalId). On idempotency-key
    collision returns the existing job.
    """
    if idempotency_key is not None:
        existing = await find_by_idempotency_key(
            organization_id=organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing

    dex_run_id = uuid4()

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    f"""
                    INSERT INTO business.exa_webset_jobs
                        (organization_id, created_by_user_id, dex_run_id,
                         description, count, criteria, enrichments, entity,
                         idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        str(organization_id),
                        str(created_by_user_id) if created_by_user_id else None,
                        str(dex_run_id),
                        description,
                        count,
                        Jsonb(criteria),
                        Jsonb(enrichments) if enrichments else None,
                        entity,
                        idempotency_key,
                    ),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                await conn.rollback()
                if idempotency_key is not None:
                    existing = await find_by_idempotency_key(
                        organization_id=organization_id,
                        idempotency_key=idempotency_key,
                    )
                    if existing is not None:
                        return existing
                raise
        await conn.commit()
    assert row is not None
    return _row_to_dict(row)


async def get_job(
    job_id: UUID,
    *,
    organization_id: UUID | None = None,
) -> dict[str, Any] | None:
    where = ["id = %s"]
    args: list[Any] = [str(job_id)]
    if organization_id is not None:
        where.append("organization_id = %s")
        args.append(str(organization_id))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM business.exa_webset_jobs "
                f"WHERE {' AND '.join(where)}",
                args,
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def append_history(
    job_id: UUID,
    event: dict[str, Any],
) -> None:
    """Best-effort append. History is observability, not control flow."""
    entry = {
        "at": datetime.now(UTC).isoformat(),
        **event,
    }
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE business.exa_webset_jobs
                    SET history = history || %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps([entry]), str(job_id)),
                )
            await conn.commit()
    except Exception:  # pragma: no cover — observability
        logger.exception(
            "exa_webset_jobs.append_history failed",
            extra={"job_id": str(job_id)},
        )


async def mark_running(
    job_id: UUID,
    trigger_run_id: str | None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_webset_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, NOW()),
                    trigger_run_id = COALESCE(%s, trigger_run_id),
                    attempts = attempts + 1
                WHERE id = %s
                """,
                (trigger_run_id, str(job_id)),
            )
        await conn.commit()
    await append_history(job_id, {"kind": "transition", "to_status": "running"})


async def mark_succeeded(
    job_id: UUID,
    *,
    exa_webset_id: str | None = None,
    result_summary: dict[str, Any] | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_webset_jobs
                SET status = 'succeeded',
                    exa_webset_id = COALESCE(%s, exa_webset_id),
                    result_summary = COALESCE(%s, result_summary),
                    completed_at = COALESCE(completed_at, NOW())
                WHERE id = %s
                """,
                (
                    exa_webset_id,
                    Jsonb(result_summary) if result_summary else None,
                    str(job_id),
                ),
            )
        await conn.commit()
    await append_history(
        job_id,
        {
            "kind": "transition",
            "to_status": "succeeded",
            "exa_webset_id": exa_webset_id,
        },
    )


async def mark_failed(
    job_id: UUID,
    error: dict[str, Any],
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_webset_jobs
                SET status = 'failed',
                    error = %s,
                    completed_at = COALESCE(completed_at, NOW())
                WHERE id = %s
                """,
                (Jsonb(error), str(job_id)),
            )
        await conn.commit()
    await append_history(
        job_id,
        {"kind": "transition", "to_status": "failed", "error": error},
    )


async def update_trigger_run_id(
    job_id: UUID,
    trigger_run_id: str,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_webset_jobs
                SET trigger_run_id = %s
                WHERE id = %s
                """,
                (trigger_run_id, str(job_id)),
            )
        await conn.commit()


async def count_runs_today(*, organization_id: UUID) -> int:
    """Count webset jobs created today (UTC) for this org."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) FROM business.exa_webset_jobs
                WHERE organization_id = %s
                  AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                (str(organization_id),),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def list_recent_jobs(
    *,
    organization_id: UUID,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the most recent webset jobs for this org (newest first)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.exa_webset_jobs
                WHERE organization_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(organization_id), limit),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


__all__ = [
    "ExaWebsetJobError",
    "ExaWebsetJobNotFound",
    "create_job",
    "get_job",
    "find_by_idempotency_key",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
    "append_history",
    "update_trigger_run_id",
    "count_runs_today",
    "list_recent_jobs",
]
