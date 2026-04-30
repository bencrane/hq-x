"""Multi-step scheduler — durable sleep + scheduled-step activation.

When step N's memberships all reach a terminal status (sent + failed +
suppressed + cancelled count == total), the step transitions to
``sent`` (or ``failed`` if all failed) and we schedule step N+1 for
``next_step.delay_days_from_previous`` days from now.

Scheduling shape:

  1. Insert a ``business.activation_jobs`` row with
     kind=``step_scheduled_activation``, payload={step_id, scheduled_for}.
  2. Enqueue Trigger.dev's ``dmaas.scheduled_step_activation`` task with
     a delay equal to the gap. The task's ``wait.for(delay)`` survives
     deploys and restarts.
  3. After the wait elapses, the task calls
     ``/internal/dmaas/process-job`` (Slice 1's existing endpoint) with
     ``{job_id}``. The job kind=``step_scheduled_activation`` branch
     activates the step.

Cancellation: pause/archive on a parent campaign or channel_campaign
must walk to all queued scheduled-activation jobs and cancel them via
``activation_jobs.cancel_job`` (which calls Trigger.dev's run-cancel
API). The wait.for() inside the task is interrupted by the cancel.

Idempotency: if ``schedule_next_step`` is called twice for the same
completed step (e.g. concurrent membership-transition handlers), the
second call returns the existing scheduled job rather than creating a
duplicate.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.models.activation_jobs import ActivationJobResponse
from app.services import activation_jobs as jobs_svc

logger = logging.getLogger(__name__)


_TASK_IDENTIFIER = "dmaas.scheduled_step_activation"
_SECONDS_PER_DAY = 86_400


class StepSchedulerError(Exception):
    pass


async def _find_next_step(
    *, completed_step_id: UUID
) -> dict[str, Any] | None:
    """Look up the next step (by step_order) within the same
    channel_campaign as the just-completed step.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.organization_id, s.brand_id, s.step_order,
                       s.delay_days_from_previous, s.status,
                       s.channel_campaign_id, s.campaign_id
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaign_steps prev
                  ON prev.id = %s
                WHERE s.channel_campaign_id = prev.channel_campaign_id
                  AND s.step_order > prev.step_order
                ORDER BY s.step_order
                LIMIT 1
                """,
                (str(completed_step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "organization_id": row[1],
        "brand_id": row[2],
        "step_order": row[3],
        "delay_days_from_previous": row[4],
        "status": row[5],
        "channel_campaign_id": row[6],
        "campaign_id": row[7],
    }


async def _find_existing_scheduled_job(
    *, step_id: UUID
) -> ActivationJobResponse | None:
    """Look up any open scheduled-activation job already covering the
    next step. Used to make schedule_next_step idempotent."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, organization_id, brand_id, kind, status,
                       idempotency_key, payload, result, error, history,
                       trigger_run_id, attempts, created_at, started_at,
                       completed_at, dead_lettered_at
                FROM business.activation_jobs
                WHERE kind = 'step_scheduled_activation'
                  AND status IN ('queued', 'running')
                  AND payload->>'step_id' = %s
                LIMIT 1
                """,
                (str(step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    history_raw = row[9] or []
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
        history=history_raw,
        trigger_run_id=row[10],
        attempts=row[11] or 0,
        created_at=row[12],
        started_at=row[13],
        completed_at=row[14],
        dead_lettered_at=row[15],
    )


async def schedule_next_step(
    *, completed_step_id: UUID
) -> ActivationJobResponse | None:
    """If the just-completed step has a successor in the same
    channel_campaign, persist a queued ``step_scheduled_activation``
    job and enqueue a Trigger.dev task with ``wait.for(delay_days)``.
    """
    next_step = await _find_next_step(completed_step_id=completed_step_id)
    if next_step is None:
        return None

    next_step_id = next_step["step_id"]
    if next_step["status"] != "pending":
        # Either already scheduled, already activated, or cancelled.
        # Don't double-schedule.
        return None

    existing = await _find_existing_scheduled_job(step_id=next_step_id)
    if existing is not None:
        return existing

    delay_days = int(next_step["delay_days_from_previous"] or 0)
    delay_seconds = max(0, delay_days * _SECONDS_PER_DAY)

    payload = {
        "step_id": str(next_step_id),
        "completed_step_id": str(completed_step_id),
        "delay_days": delay_days,
    }
    job = await jobs_svc.create_job(
        organization_id=next_step["organization_id"],
        brand_id=next_step["brand_id"],
        kind="step_scheduled_activation",
        payload=payload,
    )

    # Delay lives in the *payload*, not Trigger.dev's options.delay.
    # The task uses wait.for() inside its run() body so the cancel API
    # interrupts the sleep — Trigger.dev's options.delay would queue the
    # run before it starts, which is harder to cancel cleanly.
    try:
        run_id = await jobs_svc.enqueue_via_trigger(
            job=job,
            task_identifier=_TASK_IDENTIFIER,
            payload_override={
                "job_id": str(job.id),
                "delay_seconds": delay_seconds,
            },
        )
    except jobs_svc.TriggerEnqueueError as exc:
        await jobs_svc.transition_job(
            job_id=job.id,
            status="failed",
            error={
                "reason": "trigger_enqueue_failed",
                "message": str(exc)[:500],
            },
        )
        raise

    job = await jobs_svc.transition_job(
        job_id=job.id,
        status="queued",
        trigger_run_id=run_id,
    )

    # Best-effort analytics emit so customers / operators can see the
    # scheduling event in their stream.
    try:
        from app.services.analytics import emit_event

        await emit_event(
            event_name="step.scheduled_for_activation",
            channel_campaign_step_id=next_step_id,
            properties={
                "job_id": str(job.id),
                "delay_days": delay_days,
                "completed_step_id": str(completed_step_id),
            },
        )
    except Exception:  # pragma: no cover — observability
        logger.exception("step_scheduler.emit_event_failed")

    return job


async def cancel_scheduled_step(
    *,
    step_id: UUID,
    organization_id: UUID,
    reason: str = "campaign_paused",
) -> ActivationJobResponse | None:
    """Cancel any queued/running scheduled-activation job for this step.

    Called when a parent campaign or channel_campaign is paused or
    archived. No-op if no open scheduled job exists.
    """
    job = await _find_existing_scheduled_job(step_id=step_id)
    if job is None:
        return None
    return await jobs_svc.cancel_job(
        job_id=job.id,
        organization_id=organization_id,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Step-completion detection
# ---------------------------------------------------------------------------


async def count_step_memberships_by_status(
    *, step_id: UUID
) -> dict[str, int]:
    """Return a {status -> count} dict for every membership of a step."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, COUNT(*)::int
                FROM business.channel_campaign_step_recipients
                WHERE channel_campaign_step_id = %s
                GROUP BY status
                """,
                (str(step_id),),
            )
            rows = await cur.fetchall()
    return {row[0]: int(row[1]) for row in rows}


_TERMINAL_MEMBERSHIP_STATUSES = {"sent", "failed", "suppressed", "cancelled"}


def is_step_complete(counts: dict[str, int]) -> tuple[bool, str | None]:
    """Given a status->count dict, return (is_complete, terminal_step_status).

    ``is_complete=True`` when every membership is in a terminal state.
    Returns ``terminal_step_status='failed'`` when ALL terminal members
    failed (no successful sends), otherwise ``'sent'``.
    """
    total = sum(counts.values())
    if total == 0:
        return False, None
    terminal = sum(c for s, c in counts.items() if s in _TERMINAL_MEMBERSHIP_STATUSES)
    if terminal != total:
        return False, None
    failed = counts.get("failed", 0)
    suppressed = counts.get("suppressed", 0)
    sent = counts.get("sent", 0)
    if sent == 0 and (failed + suppressed) > 0:
        return True, "failed"
    return True, "sent"


async def maybe_complete_step_and_schedule_next(
    *, step_id: UUID
) -> dict[str, Any]:
    """Hook called after a membership transitions to a terminal status.

    If every membership is now terminal:
      * flip the step's status to sent / failed
      * emit step.completed (or step.failed)
      * if the terminal status is sent, schedule step N+1

    Idempotent: re-runs are no-ops once the step is already in a
    terminal status.
    """
    counts = await count_step_memberships_by_status(step_id=step_id)
    complete, terminal_status = is_step_complete(counts)
    if not complete:
        return {"completed": False, "counts": counts}

    from app.services.channel_campaign_steps import get_step_simple

    step_status = await get_step_simple(step_id=step_id)
    if step_status is None:
        return {"completed": False, "reason": "step_not_found"}
    if step_status in ("sent", "failed", "cancelled"):
        return {
            "completed": True,
            "terminal_status": step_status,
            "scheduled_job": None,
        }

    from app.services.channel_campaign_steps import update_step_status

    target = terminal_status or "sent"
    await update_step_status(step_id=step_id, new_status=target)

    try:
        from app.services.analytics import emit_event

        await emit_event(
            event_name="step.completed" if target == "sent" else "step.failed",
            channel_campaign_step_id=step_id,
            properties={"counts": counts, "terminal_status": target},
        )
    except Exception:  # pragma: no cover — observability
        logger.exception("step_scheduler.completion_emit_failed")

    scheduled_job: ActivationJobResponse | None = None
    if target == "sent":
        try:
            scheduled_job = await schedule_next_step(completed_step_id=step_id)
        except jobs_svc.TriggerEnqueueError:
            # Reconciliation cron picks up failed jobs; don't surface the
            # enqueue failure to the webhook flow.
            logger.exception("step_scheduler.next_step_enqueue_failed")
    return {
        "completed": True,
        "terminal_status": target,
        "scheduled_job": scheduled_job.id if scheduled_job is not None else None,
    }


__all__ = [
    "StepSchedulerError",
    "schedule_next_step",
    "cancel_scheduled_step",
    "count_step_memberships_by_status",
    "is_step_complete",
    "maybe_complete_step_and_schedule_next",
]
