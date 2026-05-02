"""CRUD + lifecycle helpers for business.exa_research_jobs.

Mirrors the activation_jobs service pattern: the Postgres row is the
source of truth, and Trigger.dev tasks call back into hq-x's
/internal/exa endpoints to drive state transitions.

Idempotency-Key contract: when ``create_job`` is called twice with the
same ``(organization_id, idempotency_key)`` for the same org, the second
call returns the existing job's row rather than spawning a duplicate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


class ExaResearchJobError(Exception):
    pass


class ExaResearchJobNotFound(ExaResearchJobError):
    pass


_COLUMNS = (
    "id, organization_id, created_by_user_id, endpoint, destination, "
    "objective, objective_ref, request_payload, status, result_ref, "
    "error, history, trigger_run_id, idempotency_key, attempts, "
    "created_at, started_at, completed_at"
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "organization_id": row[1],
        "created_by_user_id": row[2],
        "endpoint": row[3],
        "destination": row[4],
        "objective": row[5],
        "objective_ref": row[6],
        "request_payload": row[7] or {},
        "status": row[8],
        "result_ref": row[9],
        "error": row[10],
        "history": row[11] or [],
        "trigger_run_id": row[12],
        "idempotency_key": row[13],
        "attempts": row[14] or 0,
        "created_at": row[15],
        "started_at": row[16],
        "completed_at": row[17],
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
                FROM business.exa_research_jobs
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
    endpoint: str,
    destination: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Insert a new exa_research_jobs row in status='queued'.

    On idempotency-key collision returns the existing job rather than
    raising — the customer's replay should be a no-op that yields the
    same job_id.
    """
    if idempotency_key is not None:
        existing = await find_by_idempotency_key(
            organization_id=organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    f"""
                    INSERT INTO business.exa_research_jobs
                        (organization_id, created_by_user_id, endpoint,
                         destination, objective, objective_ref,
                         request_payload, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        str(organization_id),
                        str(created_by_user_id) if created_by_user_id else None,
                        endpoint,
                        destination,
                        objective,
                        objective_ref,
                        Jsonb(request_payload),
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
                f"SELECT {_COLUMNS} FROM business.exa_research_jobs "
                f"WHERE {' AND '.join(where)}",
                args,
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def append_history(
    job_id: UUID,
    event: dict[str, Any],
) -> None:
    """Best-effort append. History is observability, not control flow,
    so a failure to record never raises into the caller."""
    entry = {
        "at": datetime.now(UTC).isoformat(),
        **event,
    }
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE business.exa_research_jobs
                    SET history = history || %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps([entry]), str(job_id)),
                )
            await conn.commit()
    except Exception:  # pragma: no cover — observability
        logger.exception(
            "exa_research_jobs.append_history failed",
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
                UPDATE business.exa_research_jobs
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
    result_ref: str,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_research_jobs
                SET status = 'succeeded',
                    result_ref = %s,
                    completed_at = COALESCE(completed_at, NOW())
                WHERE id = %s
                """,
                (result_ref, str(job_id)),
            )
        await conn.commit()
    await append_history(
        job_id,
        {"kind": "transition", "to_status": "succeeded", "result_ref": result_ref},
    )


async def mark_failed(
    job_id: UUID,
    error: dict[str, Any],
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.exa_research_jobs
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
                UPDATE business.exa_research_jobs
                SET trigger_run_id = %s
                WHERE id = %s
                """,
                (trigger_run_id, str(job_id)),
            )
        await conn.commit()


__all__ = [
    "ExaResearchJobError",
    "ExaResearchJobNotFound",
    "create_job",
    "get_job",
    "find_by_idempotency_key",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
    "append_history",
    "update_trigger_run_id",
]
