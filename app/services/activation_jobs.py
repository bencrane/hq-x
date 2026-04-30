"""CRUD + lifecycle helpers for business.activation_jobs.

Job rows are the source of truth for every async DMaaS operation.
Trigger.dev tasks are thin executors that call back into hq-x's
``/internal/dmaas/process-job`` endpoint with ``{job_id, trigger_run_id}``;
this module owns all state transitions plus the wrapper around
Trigger.dev's HTTP API for enqueuing + cancelling runs.

Idempotency-Key contract: when a customer-facing async endpoint is
called twice with the same ``Idempotency-Key`` header for the same org,
the second call returns the existing job's id rather than spawning a
duplicate. The ``(organization_id, idempotency_key)`` partial unique
index in the DB enforces this.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.models.activation_jobs import (
    ActivationJobHistoryEntry,
    ActivationJobKind,
    ActivationJobResponse,
    ActivationJobStatus,
)

logger = logging.getLogger(__name__)


class ActivationJobError(Exception):
    pass


class ActivationJobNotFound(ActivationJobError):
    pass


class ActivationJobInvalidTransition(ActivationJobError):
    pass


class TriggerEnqueueError(ActivationJobError):
    """Raised when Trigger.dev rejects (or hq-x cannot reach) the trigger API."""


_COLUMNS = (
    "id, organization_id, brand_id, kind, status, idempotency_key, "
    "payload, result, error, history, trigger_run_id, attempts, "
    "created_at, started_at, completed_at, dead_lettered_at"
)


def _row_to_response(row: tuple[Any, ...]) -> ActivationJobResponse:
    history_raw = row[9] or []
    history = [
        ActivationJobHistoryEntry(**h) if isinstance(h, dict) else h for h in history_raw
    ]
    return ActivationJobResponse(
        id=row[0],
        organization_id=row[1],
        brand_id=row[2],
        kind=row[3],
        status=row[4],
        idempotency_key=row[5],
        payload=row[6] or {},
        result=row[7],
        error=row[8],
        history=history,
        trigger_run_id=row[10],
        attempts=row[11] or 0,
        created_at=row[12],
        started_at=row[13],
        completed_at=row[14],
        dead_lettered_at=row[15],
    )


async def get_job(
    *, job_id: UUID, organization_id: UUID | None = None
) -> ActivationJobResponse:
    """Fetch a single job. When ``organization_id`` is supplied, an
    org mismatch surfaces as ``ActivationJobNotFound`` (the customer-facing
    GET maps that to 404).
    """
    where = ["id = %s"]
    args: list[Any] = [str(job_id)]
    if organization_id is not None:
        where.append("organization_id = %s")
        args.append(str(organization_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM business.activation_jobs WHERE {' AND '.join(where)}",
                args,
            )
            row = await cur.fetchone()
    if row is None:
        raise ActivationJobNotFound(f"job {job_id} not found")
    return _row_to_response(row)


async def find_job_by_idempotency_key(
    *, organization_id: UUID, idempotency_key: str
) -> ActivationJobResponse | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.activation_jobs
                WHERE organization_id = %s AND idempotency_key = %s
                """,
                (str(organization_id), idempotency_key),
            )
            row = await cur.fetchone()
    return _row_to_response(row) if row is not None else None


async def create_job(
    *,
    organization_id: UUID,
    brand_id: UUID,
    kind: ActivationJobKind,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> ActivationJobResponse:
    """Insert a new job row in status='queued'.

    On idempotency-key collision returns the existing job rather than
    raising — the customer's replay should be a no-op that yields the
    same job_id.
    """
    if idempotency_key is not None:
        existing = await find_job_by_idempotency_key(
            organization_id=organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    f"""
                    INSERT INTO business.activation_jobs
                        (organization_id, brand_id, kind, status,
                         idempotency_key, payload)
                    VALUES (%s, %s, %s, 'queued', %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        str(organization_id),
                        str(brand_id),
                        kind,
                        idempotency_key,
                        Jsonb(payload),
                    ),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                # Concurrent insert with the same idempotency_key. Re-read
                # the existing row instead of raising.
                await conn.rollback()
                if idempotency_key is not None:
                    existing = await find_job_by_idempotency_key(
                        organization_id=organization_id,
                        idempotency_key=idempotency_key,
                    )
                    if existing is not None:
                        return existing
                raise
        await conn.commit()
    assert row is not None
    return _row_to_response(row)


async def append_history(
    *,
    job_id: UUID,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one entry to a job's history JSONB array. Best-effort,
    never raises into the caller — history is observability, not control
    flow."""
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "detail": detail or {},
    }
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE business.activation_jobs
                    SET history = history || %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps([entry]), str(job_id)),
                )
            await conn.commit()
    except Exception:  # pragma: no cover — observability
        logger.exception("activation_jobs.append_history failed", extra={"job_id": str(job_id)})


async def transition_job(
    *,
    job_id: UUID,
    status: ActivationJobStatus,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    trigger_run_id: str | None = None,
    increment_attempts: bool = False,
) -> ActivationJobResponse:
    """Update the job to a new status, persisting result/error/timestamp
    fields appropriate to the transition.

    ``running``  -> sets started_at if null + optional trigger_run_id
    ``succeeded``/``failed`` -> sets completed_at + result|error
    ``cancelled`` -> sets completed_at
    ``dead_lettered`` -> sets dead_lettered_at + completed_at
    """
    set_parts: list[str] = ["status = %s"]
    args: list[Any] = [status]

    if status == "running":
        set_parts.append("started_at = COALESCE(started_at, NOW())")
    if status in ("succeeded", "failed", "cancelled"):
        set_parts.append("completed_at = COALESCE(completed_at, NOW())")
    if status == "dead_lettered":
        set_parts.append("dead_lettered_at = COALESCE(dead_lettered_at, NOW())")
        set_parts.append("completed_at = COALESCE(completed_at, NOW())")

    if result is not None:
        set_parts.append("result = %s")
        args.append(Jsonb(result))
    if error is not None:
        set_parts.append("error = %s")
        args.append(Jsonb(error))
    if trigger_run_id is not None:
        set_parts.append("trigger_run_id = %s")
        args.append(trigger_run_id)
    if increment_attempts:
        set_parts.append("attempts = attempts + 1")

    args.append(str(job_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.activation_jobs
                SET {', '.join(set_parts)}
                WHERE id = %s
                RETURNING {_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise ActivationJobNotFound(f"job {job_id} not found")
    await append_history(
        job_id=job_id,
        kind="transition",
        detail={"to_status": status},
    )
    return _row_to_response(row)


async def cancel_job(
    *,
    job_id: UUID,
    organization_id: UUID,
    reason: str = "user_requested",
) -> ActivationJobResponse:
    """Cancel a queued or running job.

    Best-effort against Trigger.dev — even if the cancel API call fails,
    the local job row transitions so the customer doesn't see the job
    stuck in queued/running forever. Reconciliation cleans up Trigger
    state on the next run.
    """
    job = await get_job(job_id=job_id, organization_id=organization_id)
    if job.status not in ("queued", "running"):
        raise ActivationJobInvalidTransition(
            f"cannot cancel job in status={job.status}"
        )

    if job.trigger_run_id:
        try:
            await cancel_trigger_run(job.trigger_run_id)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning(
                "activation_jobs.cancel_trigger_run_failed",
                extra={
                    "job_id": str(job_id),
                    "trigger_run_id": job.trigger_run_id,
                    "error": str(exc)[:200],
                },
            )

    return await transition_job(
        job_id=job_id,
        status="cancelled",
        error={"reason": reason},
    )


# ---------------------------------------------------------------------------
# Trigger.dev HTTP API wrapper
# ---------------------------------------------------------------------------


_TRIGGER_API_BASE = "https://api.trigger.dev"


def _trigger_secret_or_raise() -> str:
    secret = settings.TRIGGER_API_KEY
    if not secret:
        raise TriggerEnqueueError(
            "TRIGGER_API_KEY is not configured — cannot enqueue jobs"
        )
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)


async def enqueue_via_trigger(
    *,
    job: ActivationJobResponse,
    task_identifier: str,
    delay_seconds: int | None = None,
    payload_override: dict[str, Any] | None = None,
) -> str:
    """POST to Trigger.dev's /api/v1/tasks/{taskIdentifier}/trigger.

    Returns the trigger ``run.id`` so the caller can persist it on the
    job row. Caller is responsible for transitioning the job to ``failed``
    + emitting a 503 on enqueue failure.
    """
    secret = _trigger_secret_or_raise()
    base_url = (settings.TRIGGER_API_BASE_URL or _TRIGGER_API_BASE).rstrip("/")
    url = f"{base_url}/api/v1/tasks/{task_identifier}/trigger"

    body: dict[str, Any] = {
        "payload": payload_override
        if payload_override is not None
        else {"job_id": str(job.id)},
    }
    if delay_seconds is not None and delay_seconds > 0:
        body["options"] = {"delay": f"{delay_seconds}s"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise TriggerEnqueueError(
            f"trigger.dev returned {resp.status_code}: {resp.text[:500]}"
        )
    data = resp.json()
    run_id = data.get("id") or data.get("runId") or ""
    if not run_id:
        raise TriggerEnqueueError(
            f"trigger.dev response missing run id: {data!r}"
        )
    return run_id


async def cancel_trigger_run(run_id: str) -> None:
    secret = _trigger_secret_or_raise()
    base_url = (settings.TRIGGER_API_BASE_URL or _TRIGGER_API_BASE).rstrip("/")
    url = f"{base_url}/api/v2/runs/{run_id}/cancel"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {secret}",
            },
        )
    if resp.status_code >= 400:
        raise TriggerEnqueueError(
            f"trigger.dev cancel returned {resp.status_code}: {resp.text[:500]}"
        )


__all__ = [
    "ActivationJobError",
    "ActivationJobNotFound",
    "ActivationJobInvalidTransition",
    "TriggerEnqueueError",
    "create_job",
    "get_job",
    "find_job_by_idempotency_key",
    "transition_job",
    "append_history",
    "cancel_job",
    "enqueue_via_trigger",
    "cancel_trigger_run",
]
