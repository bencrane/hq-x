"""Public Exa Websets dataset-builder control surface.

POST /api/v1/exa/websets — enqueue an async Exa Websets run (202 + job_id).
GET  /api/v1/exa/websets/{job_id} — poll job status.

Server-side cap enforcement (per directive §Constraints):
  count      ≤ 25  → 400
  criteria   ≤ 5   → 400
  enrichments ≤ 3  → 400
  daily cap  (EXA_WEBSETS_DAILY_RUN_CAP, default 15) → 429 on 16th run-of-day

The dex_run_id is minted here and passed to Trigger as the payload ID.
Trigger calls back to /internal/exa/websets/{job_id}/process, which creates
the webset on Exa (using dex_run_id as externalId) and polls until completion.
Do NOT modify apps/hq-x/app/routers/exa_jobs.py — sibling router only.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.config import settings
from app.services import activation_jobs as jobs_svc
from app.services import exa_webset_jobs as webset_jobs_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/exa", tags=["exa-websets"])

_TASK_IDENTIFIER = "exa.process_webset_job"

# Hard caps — also enforced in the agent system prompt.
_MAX_COUNT = 25
_MAX_CRITERIA = 5
_MAX_ENRICHMENTS = 3


class ExaWebsetCriterion(BaseModel):
    """Single webset search criterion, forwarded verbatim to Exa."""

    type: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)
    model_config = {"extra": "allow"}


class ExaWebsetEnrichment(BaseModel):
    """Single enrichment column definition, forwarded verbatim to Exa."""

    description: str = Field(min_length=1, max_length=500)
    model_config = {"extra": "allow"}


class CreateExaWebsetRequest(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    count: int = Field(ge=1, le=_MAX_COUNT)
    criteria: list[ExaWebsetCriterion] = Field(min_length=1, max_length=_MAX_CRITERIA)
    enrichments: list[ExaWebsetEnrichment] | None = Field(
        default=None, max_length=_MAX_ENRICHMENTS
    )
    entity: str = Field(default="company", max_length=50)
    idempotency_key: str | None = None
    model_config = {"extra": "forbid"}

    @field_validator("count")
    @classmethod
    def count_le_max(cls, v: int) -> int:
        if v > _MAX_COUNT:
            raise ValueError(f"count must be ≤ {_MAX_COUNT} (Exa free-tier cap)")
        return v


class ExaWebsetAcceptedResponse(BaseModel):
    job_id: UUID
    dex_run_id: UUID
    status: str


def _not_found(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


@router.post(
    "/websets",
    response_model=ExaWebsetAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_exa_webset(
    body: CreateExaWebsetRequest,
    user: UserContext = Depends(require_org_context),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExaWebsetAcceptedResponse:
    """Enqueue an Exa Websets dataset-builder run.

    Hard caps enforced here (count ≤ 25, criteria ≤ 5, enrichments ≤ 3).
    Per-day run cap enforced by counting today's rows for this org.
    Returns 202 with job_id + dex_run_id; caller polls GET /api/v1/exa/websets/{job_id}.
    On Idempotency-Key replay, returns the same job without spawning a duplicate.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    user_id = (
        user.business_user_id
        if hasattr(user, "business_user_id") and user.business_user_id is not None
        else None
    )

    idem = body.idempotency_key or idempotency_key_header

    # Daily cap check — runs before insert to avoid burning the slot.
    today_count = await webset_jobs_svc.count_runs_today(organization_id=org_id)
    if today_count >= settings.EXA_WEBSETS_DAILY_RUN_CAP:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "daily_cap_exceeded",
                "message": (
                    f"You have already run {today_count} websets today. "
                    f"The daily cap is {settings.EXA_WEBSETS_DAILY_RUN_CAP}."
                ),
                "cap": settings.EXA_WEBSETS_DAILY_RUN_CAP,
                "today_count": today_count,
            },
        )

    job = await webset_jobs_svc.create_job(
        organization_id=org_id,
        created_by_user_id=user_id,
        description=body.description,
        count=body.count,
        criteria=[c.model_dump() for c in body.criteria],
        enrichments=[e.model_dump() for e in body.enrichments] if body.enrichments else None,
        entity=body.entity,
        idempotency_key=idem,
    )

    # Replay short-circuit: if a trigger run is already enqueued, surface
    # the existing job without re-queuing.
    if job.get("trigger_run_id"):
        return ExaWebsetAcceptedResponse(
            job_id=job["id"], dex_run_id=job["dex_run_id"], status=job["status"]
        )

    try:
        run_id = await jobs_svc.enqueue_via_trigger(
            task_identifier=_TASK_IDENTIFIER,
            payload_override={"job_id": str(job["id"])},
        )
    except jobs_svc.TriggerEnqueueError as exc:
        await webset_jobs_svc.mark_failed(
            job["id"],
            error={"reason": "trigger_enqueue_failed", "message": str(exc)[:500]},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "job_enqueue_failed",
                "message": "Could not schedule the Exa Websets job. Try again.",
                "job_id": str(job["id"]),
            },
        ) from exc

    await webset_jobs_svc.update_trigger_run_id(job["id"], run_id)

    return ExaWebsetAcceptedResponse(
        job_id=job["id"], dex_run_id=job["dex_run_id"], status="queued"
    )


@router.get("/websets/{job_id}")
async def get_exa_webset(
    job_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> dict[str, Any]:
    """Return the full webset job row. Cross-org access surfaces as 404."""
    org_id = user.active_organization_id
    assert org_id is not None
    job = await webset_jobs_svc.get_job(job_id, organization_id=org_id)
    if job is None:
        raise _not_found("webset_job_not_found")
    return job


@router.get("/websets")
async def list_exa_websets(
    user: UserContext = Depends(require_org_context),
    limit: int = 10,
) -> dict[str, Any]:
    """List the most recent webset jobs for this org (newest first).

    Drives the Data Work admin page's "last run" panel — single-operator
    self-prospect, so a small limit is plenty.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    capped = max(1, min(limit, 50))
    jobs = await webset_jobs_svc.list_recent_jobs(
        organization_id=org_id, limit=capped
    )
    return {"jobs": jobs, "limit": capped}


__all__ = ["router"]
