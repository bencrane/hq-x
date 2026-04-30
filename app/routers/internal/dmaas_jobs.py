"""Internal job-processing endpoint driven by Trigger.dev tasks.

The Trigger.dev task ``dmaas.process_activation_job`` calls this with
``{job_id, trigger_run_id}`` after enqueue. This endpoint dispatches by
``job.kind``:

  * ``dmaas_campaign_activation``   - run the opinionated DMaaS pipeline
  * ``step_activation``             - activate a single step (Slice 4 reuses)
  * ``step_scheduled_activation``   - durable-sleep next-step activation (Slice 4)

On success the job transitions to ``succeeded`` with the result JSON; on
failure to ``failed`` with the error JSON. The endpoint **re-raises the
underlying exception** so Trigger.dev's task-level retry policy can
re-fire (max 3 attempts; per ``trigger.config.ts``). The Postgres job
row is the source of truth either way.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.models.recipients import RecipientSpec
from app.services import activation_jobs as jobs_svc
from app.services.dmaas_campaign_activation import (
    DMaaSActivationError,
    run_campaign_activation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dmaas", tags=["internal"])


def _parse_uuid(value: Any | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


@router.post(
    "/process-job",
    dependencies=[Depends(verify_trigger_secret)],
)
async def process_dmaas_job(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    job_id_raw = body.get("job_id")
    if job_id_raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "missing_job_id"},
        )
    job_id = UUID(str(job_id_raw))
    trigger_run_id = body.get("trigger_run_id")

    try:
        job = await jobs_svc.get_job(job_id=job_id)
    except jobs_svc.ActivationJobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        ) from exc

    # If the job is already terminal, return idempotently — the task may
    # have been retried after we already finished, or the user may have
    # cancelled mid-run.
    if job.status in ("succeeded", "failed", "cancelled", "dead_lettered"):
        return {
            "job_id": str(job.id),
            "status": job.status,
            "skipped": True,
            "reason": "job_already_terminal",
        }

    job = await jobs_svc.transition_job(
        job_id=job.id,
        status="running",
        trigger_run_id=trigger_run_id if isinstance(trigger_run_id, str) else None,
        increment_attempts=True,
    )

    if job.kind == "dmaas_campaign_activation":
        return await _process_campaign_activation(job_id=job.id, payload=job.payload)
    if job.kind == "step_activation":
        return await _process_step_activation(job_id=job.id, payload=job.payload)
    if job.kind == "step_scheduled_activation":
        return await _process_step_activation(job_id=job.id, payload=job.payload)

    # Unknown kind — fail explicitly so we don't loop forever.
    await jobs_svc.transition_job(
        job_id=job.id,
        status="failed",
        error={"reason": "unknown_job_kind", "kind": job.kind},
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "unknown_job_kind", "kind": job.kind},
    )


async def _process_campaign_activation(
    *, job_id: UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    organization_id = _parse_uuid(payload.get("organization_id"))
    brand_id = _parse_uuid(payload.get("brand_id"))
    if brand_id is None:
        await jobs_svc.transition_job(
            job_id=job_id,
            status="failed",
            error={"reason": "missing_brand_id"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "missing_brand_id"},
        )
    if organization_id is None:
        # Re-read from job row.
        job = await jobs_svc.get_job(job_id=job_id)
        organization_id = job.organization_id

    user_id = _parse_uuid(payload.get("user_id"))
    recipients_raw = payload.get("recipients") or []
    recipients = [
        RecipientSpec(**r) if isinstance(r, dict) else r for r in recipients_raw
    ]

    send_date = payload.get("send_date")
    if isinstance(send_date, str):
        from datetime import date as _date

        send_date = _date.fromisoformat(send_date)

    try:
        result = await run_campaign_activation(
            organization_id=organization_id,
            user_id=user_id,
            name=payload["name"],
            brand_id=brand_id,
            description=payload.get("description"),
            send_date=send_date,
            creative_payload=payload.get("creative_payload") or {},
            use_landing_page=bool(payload.get("use_landing_page", True)),
            landing_page_config=payload.get("landing_page_config"),
            destination_url_override=payload.get("destination_url_override"),
            recipients=recipients,
        )
    except DMaaSActivationError as exc:
        await jobs_svc.transition_job(
            job_id=job_id,
            status="failed",
            error={
                "reason": exc.error_code,
                "message": str(exc),
                "detail": exc.detail,
            },
        )
        # Do NOT re-raise — this is a deterministic business-logic failure;
        # retrying the same job won't help. Customer sees status=failed.
        return {"job_id": str(job_id), "status": "failed", "error": exc.error_code}
    except Exception as exc:
        # Transient infra error — re-raise so Trigger.dev's retry policy
        # picks it up. Persist the partial error so observability is intact.
        logger.exception("dmaas.process_job activation crashed")
        await jobs_svc.append_history(
            job_id=job_id,
            kind="retry",
            detail={"error": str(exc)[:500]},
        )
        raise

    await jobs_svc.transition_job(
        job_id=job_id,
        status="succeeded",
        result=result,
    )
    return {"job_id": str(job_id), "status": "succeeded"}


async def _process_step_activation(
    *, job_id: UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    """Activate a single channel_campaign_step.

    Slice 1 ships the dispatch + transition wiring; Slice 4 fills in
    the multi-step scheduler that actually creates ``step_activation``
    and ``step_scheduled_activation`` jobs.
    """
    step_id = _parse_uuid(payload.get("step_id"))
    if step_id is None:
        await jobs_svc.transition_job(
            job_id=job_id,
            status="failed",
            error={"reason": "missing_step_id"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "missing_step_id"},
        )
    job = await jobs_svc.get_job(job_id=job_id)
    organization_id = job.organization_id

    from app.services import channel_campaign_steps as steps_svc

    try:
        activated = await steps_svc.activate_step(
            step_id=step_id, organization_id=organization_id
        )
    except (
        steps_svc.StepActivationNotImplemented,
        steps_svc.StepInvalidStatusTransition,
        steps_svc.StepNotFound,
    ) as exc:
        await jobs_svc.transition_job(
            job_id=job_id,
            status="failed",
            error={"reason": type(exc).__name__, "message": str(exc)},
        )
        return {"job_id": str(job_id), "status": "failed"}
    except Exception as exc:
        logger.exception("dmaas.process_job step activation crashed")
        await jobs_svc.append_history(
            job_id=job_id,
            kind="retry",
            detail={"error": str(exc)[:500]},
        )
        raise

    await jobs_svc.transition_job(
        job_id=job_id,
        status="succeeded",
        result={
            "step_id": str(activated.id),
            "status": activated.status,
            "external_provider_id": activated.external_provider_id,
        },
    )
    return {"job_id": str(job_id), "status": "succeeded"}


__all__ = ["router"]
