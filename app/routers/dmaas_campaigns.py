"""Async opinionated single-call DMaaS API + activation-job control surface.

The customer-facing convenience surface for "we run your direct mail."
The single POST collapses the five-call create+activate flow:

  POST campaign
  POST channel-campaign
  POST step
  materialize audience
  POST step/activate

Slice 1 (this directive) makes the endpoint **async-only**: instead of
running the 15-60s pipeline inline on the HTTP worker, a row is written
to ``business.activation_jobs`` and a Trigger.dev task picks it up.
The customer polls ``GET /api/v1/dmaas/jobs/{job_id}`` (or subscribes
to a webhook subscription, Slice 2) for completion.

Breaking change for any caller that relied on synchronous return
semantics. There is no sync compatibility shim — Directive 3 §1.3
takes the breaking change explicitly because the customer count today
is zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.activation_jobs import ActivationJobResponse
from app.models.campaigns import StepLandingPageConfig
from app.models.recipients import RecipientSpec
from app.services import activation_jobs as jobs_svc
from app.services import campaigns as campaigns_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dmaas", tags=["dmaas"])


_RECIPIENT_CAP = 50_000
_TASK_IDENTIFIER_DMAAS_PROCESS = "dmaas.process_activation_job"


class DMaaSCreative(BaseModel):
    lob_creative_payload: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class DMaaSCampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brand_id: UUID
    send_date: date | None = None
    description: str | None = Field(default=None, max_length=2000)

    creative: DMaaSCreative
    landing_page: StepLandingPageConfig | None = None
    use_landing_page: bool = True
    destination_url_override: str | None = Field(default=None, max_length=2048)

    recipients: list[RecipientSpec] = Field(min_length=1, max_length=_RECIPIENT_CAP)

    model_config = {"extra": "forbid"}

    @field_validator("destination_url_override")
    @classmethod
    def _override_must_be_https(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("https://", "http://")):
            raise ValueError("destination_url_override must include http(s):// scheme")
        return v

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        if self.use_landing_page and self.landing_page is None:
            raise ValueError(
                "landing_page is required when use_landing_page=True"
            )
        if not self.use_landing_page and not self.destination_url_override:
            raise ValueError(
                "destination_url_override is required when use_landing_page=False"
            )
        if self.use_landing_page and self.destination_url_override:
            raise ValueError(
                "destination_url_override is mutually exclusive with use_landing_page=True"
            )


class DMaaSCampaignAcceptedResponse(BaseModel):
    """202 response from the async POST /campaigns. The customer polls
    GET /jobs/{job_id} (or subscribes to a webhook) for terminal state."""

    job_id: UUID
    status: str = "queued"


def _bad_request(error: str, message: str = "") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _not_found(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


@router.post(
    "/campaigns",
    response_model=DMaaSCampaignAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_dmaas_campaign(
    body: DMaaSCampaignCreateRequest,
    user: UserContext = Depends(require_org_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DMaaSCampaignAcceptedResponse:
    """Enqueue a DMaaS campaign activation job.

    Validates that the requesting user's org owns the brand (cheap DB
    read) before persisting the job row + enqueuing the Trigger.dev
    task. Returns 202 with the job id immediately; the caller polls
    ``GET /jobs/{job_id}`` for terminal state.

    On Idempotency-Key replay, returns the same job_id without spawning
    a duplicate.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    user_id = (
        user.business_user_id
        if hasattr(user, "business_user_id") and user.business_user_id is not None
        else None
    )

    # Cheap pre-flight: confirm brand belongs to org. Avoids enqueuing
    # work we know will fail. The full activation pipeline re-validates
    # but we want the customer to see 404 immediately on a bad brand_id.
    try:
        await campaigns_svc.assert_brand_in_organization(
            brand_id=body.brand_id, organization_id=org_id
        )
    except campaigns_svc.CampaignBrandMismatch as exc:
        raise _not_found("brand_not_found") from exc

    # Persist the job row. Recipients + creative + landing-page config
    # are serialized into payload — the internal worker re-hydrates them.
    payload = {
        "name": body.name,
        "brand_id": str(body.brand_id),
        "send_date": body.send_date.isoformat() if body.send_date else None,
        "description": body.description,
        "creative_payload": body.creative.lob_creative_payload,
        "use_landing_page": body.use_landing_page,
        "landing_page_config": (
            body.landing_page.model_dump(exclude_none=True)
            if body.landing_page is not None
            else None
        ),
        "destination_url_override": body.destination_url_override,
        "recipients": [r.model_dump() for r in body.recipients],
        "user_id": str(user_id) if user_id is not None else None,
    }

    job = await jobs_svc.create_job(
        organization_id=org_id,
        brand_id=body.brand_id,
        kind="dmaas_campaign_activation",
        payload=payload,
        idempotency_key=idempotency_key,
    )

    # On replay we may already have a trigger_run_id — short-circuit to
    # avoid re-enqueuing the task.
    if job.trigger_run_id:
        return DMaaSCampaignAcceptedResponse(job_id=job.id, status=job.status)

    try:
        run_id = await jobs_svc.enqueue_via_trigger(
            job=job, task_identifier=_TASK_IDENTIFIER_DMAAS_PROCESS
        )
    except jobs_svc.TriggerEnqueueError as exc:
        await jobs_svc.transition_job(
            job_id=job.id,
            status="failed",
            error={"reason": "trigger_enqueue_failed", "message": str(exc)[:500]},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "job_enqueue_failed",
                "message": "Could not schedule the activation job. Try again.",
                "job_id": str(job.id),
            },
        ) from exc

    job = await jobs_svc.transition_job(
        job_id=job.id,
        status="queued",
        trigger_run_id=run_id,
    )

    return DMaaSCampaignAcceptedResponse(job_id=job.id, status=job.status)


@router.get(
    "/jobs/{job_id}",
    response_model=ActivationJobResponse,
)
async def get_dmaas_job(
    job_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ActivationJobResponse:
    """Return the full job row. Cross-org access is blocked: a job
    belonging to org B is not visible to org A even if both share a
    platform_operator user."""
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await jobs_svc.get_job(job_id=job_id, organization_id=org_id)
    except jobs_svc.ActivationJobNotFound as exc:
        raise _not_found("job_not_found") from exc


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ActivationJobResponse,
)
async def cancel_dmaas_job(
    job_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ActivationJobResponse:
    """Best-effort cancel a queued/running job. The Trigger.dev run is
    cancelled in tandem; if the call fails, the local row still
    transitions so the customer doesn't see a stuck job."""
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await jobs_svc.cancel_job(
            job_id=job_id, organization_id=org_id, reason="user_requested"
        )
    except jobs_svc.ActivationJobNotFound as exc:
        raise _not_found("job_not_found") from exc
    except jobs_svc.ActivationJobInvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "invalid_status_transition",
                "message": str(exc),
            },
        ) from exc


__all__ = ["router"]


# Module-importable timestamp helpers preserved for tests that monkeypatch
# datetime.now for deterministic scheduled_send_at output.
_now = datetime.now  # noqa: F841
_today = lambda: datetime.now(UTC).date()  # noqa: E731
