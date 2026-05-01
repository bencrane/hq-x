"""Public REST surface for GTM initiatives.

A GTM initiative ties together (brand, demand-side partner, partner
contract, frozen DEX audience spec, partner-research run) and drives
two async subagents:

  1. Strategic-context research — second Exa research run, audience-scoped
     and operator-voice-sourced. Reuses business.exa_research_jobs.
  2. Strategy synthesis — first hq-x → Anthropic call. Emits
     data/initiatives/<id>/campaign_strategy.md.

Subagents 3–7 (channel materializer, audience materializer, per-recipient
creative, landing pages, voice agent) are out of scope for this slice.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.services import activation_jobs as jobs_svc
from app.services import gtm_initiatives as gtm_svc
from app.services import strategic_context_researcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/initiatives", tags=["gtm-initiatives"])


_SYNTHESIZE_TASK_IDENTIFIER = "gtm.synthesize_initiative_strategy"


class CreateInitiativeRequest(BaseModel):
    brand_id: UUID
    partner_id: UUID
    partner_contract_id: UUID
    data_engine_audience_id: UUID
    partner_research_ref: str | None = None
    reservation_window_start: datetime | None = None
    reservation_window_end: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class InitiativeResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID
    partner_id: UUID
    partner_contract_id: UUID
    data_engine_audience_id: UUID
    partner_research_ref: str | None
    strategic_context_research_ref: str | None
    campaign_strategy_path: str | None
    status: str
    history: list[dict[str, Any]]
    metadata: dict[str, Any]
    reservation_window_start: datetime | None
    reservation_window_end: datetime | None
    created_at: datetime
    updated_at: datetime


class StrategicResearchAcceptedResponse(BaseModel):
    exa_job_id: UUID
    status: str


class SynthesisAcceptedResponse(BaseModel):
    job_id: UUID
    status: str


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "initiative_not_found"},
    )


def _conflict(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "invalid_initiative_state", "message": message},
    )


@router.post(
    "",
    response_model=InitiativeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_initiative(
    body: CreateInitiativeRequest,
    user: UserContext = Depends(require_org_context),
) -> InitiativeResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    row = await gtm_svc.create_initiative(
        organization_id=org_id,
        brand_id=body.brand_id,
        partner_id=body.partner_id,
        partner_contract_id=body.partner_contract_id,
        data_engine_audience_id=body.data_engine_audience_id,
        partner_research_ref=body.partner_research_ref,
        reservation_window_start=body.reservation_window_start,
        reservation_window_end=body.reservation_window_end,
        metadata=body.metadata,
    )
    return InitiativeResponse(**row)


@router.get(
    "/{initiative_id}",
    response_model=InitiativeResponse,
)
async def get_initiative(
    initiative_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> InitiativeResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    row = await gtm_svc.get_initiative(initiative_id, organization_id=org_id)
    if row is None:
        raise _not_found()
    return InitiativeResponse(**row)


@router.post(
    "/{initiative_id}/run-strategic-research",
    response_model=StrategicResearchAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_strategic_research(
    initiative_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> StrategicResearchAcceptedResponse:
    """Fire subagent 1.

    Builds the audience-scoped, operator-voice-sourced research
    instructions, creates an exa_research_jobs row, transitions the
    initiative to `awaiting_strategic_research`, and lets the existing
    `exa.process_research_job` Trigger task drive it. The post-process
    dispatcher in /internal/exa/jobs/{id}/process detects the
    `objective='strategic_context_research'` row on completion and
    flips the initiative to `strategic_research_ready`.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    initiative = await gtm_svc.get_initiative(initiative_id, organization_id=org_id)
    if initiative is None:
        raise _not_found()
    if initiative["status"] not in ("draft", "failed"):
        raise _conflict(
            f"cannot start strategic research from status={initiative['status']!r}"
        )

    user_id = (
        user.business_user_id
        if hasattr(user, "business_user_id") and user.business_user_id is not None
        else None
    )

    try:
        result = await strategic_context_researcher.run_strategic_context_research(
            initiative_id=initiative_id,
            organization_id=org_id,
            created_by_user_id=user_id,
        )
    except strategic_context_researcher.StrategicContextResearcherError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "strategic_context_researcher_failed",
                "message": str(exc)[:500],
            },
        ) from exc
    except gtm_svc.InvalidStatusTransition as exc:
        raise _conflict(str(exc)) from exc

    return StrategicResearchAcceptedResponse(
        exa_job_id=result["exa_job_id"],
        status=result["status"],
    )


@router.post(
    "/{initiative_id}/synthesize-strategy",
    response_model=SynthesisAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def synthesize_strategy(
    initiative_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> SynthesisAcceptedResponse:
    """Fire subagent 2.

    Refuses with 409 if subagent 1 has not completed (i.e.
    ``strategic_context_research_ref`` is null). Otherwise enqueues the
    Trigger.dev task that calls back into
    /internal/initiatives/{id}/process-synthesis.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    initiative = await gtm_svc.get_initiative(initiative_id, organization_id=org_id)
    if initiative is None:
        raise _not_found()
    if initiative["status"] not in ("strategic_research_ready", "failed"):
        raise _conflict(
            f"cannot synthesize strategy from status={initiative['status']!r}"
        )
    if not initiative.get("strategic_context_research_ref"):
        raise _conflict(
            "strategic_context_research_ref is null; "
            "run /run-strategic-research first"
        )

    try:
        await gtm_svc.transition_status(
            initiative_id,
            new_status="awaiting_strategy_synthesis",
            history_event={
                "kind": "transition",
                "trigger": "synthesize-strategy endpoint",
            },
        )
    except gtm_svc.InvalidStatusTransition as exc:
        raise _conflict(str(exc)) from exc

    try:
        run_id = await jobs_svc.enqueue_via_trigger(
            task_identifier=_SYNTHESIZE_TASK_IDENTIFIER,
            payload_override={"initiative_id": str(initiative_id)},
        )
    except jobs_svc.TriggerEnqueueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "synthesis_enqueue_failed",
                "message": str(exc)[:500],
                "initiative_id": str(initiative_id),
            },
        ) from exc

    await gtm_svc.append_history(
        initiative_id,
        {
            "kind": "synthesis_enqueued",
            "trigger_run_id": run_id,
        },
    )

    return SynthesisAcceptedResponse(
        job_id=initiative_id,
        status="queued",
    )


__all__ = ["router"]
