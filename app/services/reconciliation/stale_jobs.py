"""Reconcile stale activation_jobs.

Two passes:

1. Jobs in ``running`` for longer than the configured threshold get
   marked ``failed`` with reason=``stale_running_state``. Trigger.dev
   may have lost the run, the worker may have crashed mid-flight, or
   the host process restarted.

2. Jobs in ``failed`` for longer than the dead-letter delay get
   transitioned to ``dead_lettered`` so operators have a bounded
   review queue.

Each row touched emits an analytics event ``reconciliation.drift_found``
so the operator dashboard can surface drift counts.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.services import activation_jobs as jobs_svc
from app.services.reconciliation import ReconciliationResult

logger = logging.getLogger(__name__)


async def reconcile(*, organization_id: UUID | None = None) -> ReconciliationResult:
    if not settings.DMAAS_RECONCILE_STALE_JOBS_ENABLED:
        return ReconciliationResult(enabled=False)

    result = ReconciliationResult()
    threshold_hours = settings.DMAAS_RECONCILE_STALE_JOB_THRESHOLD_HOURS
    dead_letter_hours = settings.DMAAS_RECONCILE_DEAD_LETTER_DELAY_HOURS

    # Pass 1: running > threshold → failed.
    where_running = ["status = 'running'"]
    args_running: list[Any] = []
    where_running.append(f"started_at < NOW() - INTERVAL '{int(threshold_hours)} hours'")
    if organization_id is not None:
        where_running.append("organization_id = %s")
        args_running.append(str(organization_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, organization_id, kind, started_at
                FROM business.activation_jobs
                WHERE {' AND '.join(where_running)}
                ORDER BY started_at
                LIMIT 200
                """,
                args_running,
            )
            stale_running = await cur.fetchall()

    for row in stale_running:
        result.rows_scanned += 1
        job_id, org_id, kind, started_at = row
        try:
            await jobs_svc.transition_job(
                job_id=job_id,
                status="failed",
                error={"reason": "stale_running_state", "started_at": started_at.isoformat()},
            )
            result.rows_touched += 1
            result.add_drift(
                kind="stale_running_job",
                job_id=str(job_id),
                organization_id=str(org_id),
                job_kind=kind,
            )
        except Exception:  # pragma: no cover — best-effort
            logger.exception(
                "reconcile.stale_jobs.transition_failed",
                extra={"job_id": str(job_id)},
            )

    # Pass 2: failed > dead_letter_hours → dead_lettered.
    where_failed = ["status = 'failed'"]
    args_failed: list[Any] = []
    where_failed.append(f"completed_at < NOW() - INTERVAL '{int(dead_letter_hours)} hours'")
    if organization_id is not None:
        where_failed.append("organization_id = %s")
        args_failed.append(str(organization_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, organization_id, kind
                FROM business.activation_jobs
                WHERE {' AND '.join(where_failed)}
                ORDER BY completed_at
                LIMIT 200
                """,
                args_failed,
            )
            stale_failed = await cur.fetchall()

    for row in stale_failed:
        result.rows_scanned += 1
        job_id, org_id, kind = row
        try:
            await jobs_svc.transition_job(
                job_id=job_id,
                status="dead_lettered",
                error={"reason": "dead_letter_after_delay"},
            )
            result.rows_touched += 1
            result.add_drift(
                kind="dead_lettered_job",
                job_id=str(job_id),
                organization_id=str(org_id),
                job_kind=kind,
            )
        except Exception:  # pragma: no cover — best-effort
            logger.exception(
                "reconcile.stale_jobs.dead_letter_failed",
                extra={"job_id": str(job_id)},
            )

    return result


__all__ = ["reconcile"]
