"""Public Exa research job control surface.

POST /api/v1/exa/jobs is async-only: it returns 202 with a job_id and
a Trigger.dev task drives the work. The destination (hqx | dex) is a
per-run flag set by the caller, not a global config.

Customer-facing surface is intentionally one async endpoint plus a
read endpoint. There is no public passthrough to Exa — the API key
never leaves the server.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.services import activation_jobs as jobs_svc
from app.services import exa_research_jobs as exa_jobs_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/exa", tags=["exa"])

_TASK_IDENTIFIER = "exa.process_research_job"


class CreateExaJobRequest(BaseModel):
    endpoint: Literal["search", "contents", "find_similar", "research", "answer"]
    destination: Literal["hqx", "dex"]
    objective: str = Field(min_length=1, max_length=200)
    objective_ref: str | None = Field(default=None, max_length=400)
    request_payload: dict[str, Any]
    idempotency_key: str | None = None
    model_config = {"extra": "forbid"}


class ExaJobAcceptedResponse(BaseModel):
    job_id: UUID
    status: str


def _not_found(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


@router.post(
    "/jobs",
    response_model=ExaJobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_exa_job(
    body: CreateExaJobRequest,
    user: UserContext = Depends(require_org_context),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Enqueue an Exa research job.

    The body is forwarded as-is to Exa via the worker — we never inspect
    or transform it inside the persistence path. The caller polls
    ``GET /api/v1/exa/jobs/{job_id}`` for terminal state.

    On Idempotency-Key replay (header or body), returns the same job_id
    without spawning a duplicate.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    user_id = (
        user.business_user_id
        if hasattr(user, "business_user_id") and user.business_user_id is not None
        else None
    )

    idem = body.idempotency_key or idempotency_key_header

    job = await exa_jobs_svc.create_job(
        organization_id=org_id,
        created_by_user_id=user_id,
        endpoint=body.endpoint,
        destination=body.destination,
        objective=body.objective,
        objective_ref=body.objective_ref,
        request_payload=body.request_payload,
        idempotency_key=idem,
    )

    # Replay short-circuit: if a trigger run is already enqueued, surface
    # the existing job without re-queuing.
    if job.get("trigger_run_id"):
        return ExaJobAcceptedResponse(job_id=job["id"], status=job["status"])

    try:
        run_id = await jobs_svc.enqueue_via_trigger(
            task_identifier=_TASK_IDENTIFIER,
            payload_override={"job_id": str(job["id"])},
        )
    except jobs_svc.TriggerEnqueueError as exc:
        await exa_jobs_svc.mark_failed(
            job["id"],
            error={"reason": "trigger_enqueue_failed", "message": str(exc)[:500]},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "job_enqueue_failed",
                "message": "Could not schedule the Exa research job. Try again.",
                "job_id": str(job["id"]),
            },
        ) from exc

    await exa_jobs_svc.update_trigger_run_id(job["id"], run_id)

    return ExaJobAcceptedResponse(job_id=job["id"], status="queued")


@router.get(
    "/jobs/{job_id}",
)
async def get_exa_job(
    job_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> dict[str, Any]:
    """Return the full job row. Cross-org access surfaces as 404."""
    org_id = user.active_organization_id
    assert org_id is not None
    job = await exa_jobs_svc.get_job(job_id, organization_id=org_id)
    if job is None:
        raise _not_found("job_not_found")
    return job


__all__ = ["router"]
