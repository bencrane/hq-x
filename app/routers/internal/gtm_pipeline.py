"""Internal endpoints called by the Trigger.dev gtm-run-initiative-pipeline
task. Bearer-authenticated with TRIGGER_SHARED_SECRET, same pattern as
internal/exa_jobs.py + internal/gtm_initiatives.py.

The single source of truth for an agent invocation is
``POST /run-step``: one HTTP call per agent slug, hq-x blocks for the
full Anthropic round trip, every state mutation lands in the DB before
the response returns. Trigger.dev's TS layer holds zero business state.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.services import gtm_pipeline as pipeline
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gtm", tags=["internal"])


@router.post(
    "/initiatives/{initiative_id}/run-step",
    dependencies=[Depends(verify_trigger_secret)],
)
async def run_step(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    agent_slug = body.get("agent_slug")
    if not agent_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "agent_slug_required"},
        )

    try:
        result = await pipeline.run_step(
            initiative_id=initiative_id,
            agent_slug=agent_slug,
            hint=body.get("hint"),
            upstream_outputs=body.get("upstream_outputs"),
        )
    except pipeline.AgentSlugNotRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "agent_not_registered", "message": str(exc)},
        ) from exc
    except pipeline.RunStepError as exc:
        # Anthropic-side or parse-irrecoverable failure. The DB row was
        # already finalized to status='failed' inside run_step. Re-raise
        # as 500 so Trigger.dev's task layer sees the failure and marks
        # the pipeline failed.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "run_step_failed", "message": str(exc)},
        ) from exc

    return result


@router.post(
    "/initiatives/{initiative_id}/pipeline-completed",
    dependencies=[Depends(verify_trigger_secret)],
)
async def pipeline_completed(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )
    await pipeline.set_pipeline_status(initiative_id, "completed")
    await gtm_svc.append_history(
        initiative_id,
        {
            "kind": "pipeline_completed",
            "trigger_run_id": body.get("trigger_run_id"),
        },
    )
    return {"initiative_id": str(initiative_id), "pipeline_status": "completed"}


@router.post(
    "/initiatives/{initiative_id}/pipeline-failed",
    dependencies=[Depends(verify_trigger_secret)],
)
async def pipeline_failed(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )
    await pipeline.set_pipeline_status(initiative_id, "failed")
    await gtm_svc.append_history(
        initiative_id,
        {
            "kind": "pipeline_failed",
            "trigger_run_id": body.get("trigger_run_id"),
            "failed_at_slug": body.get("failed_at_slug"),
            "reason": body.get("reason"),
        },
    )
    return {
        "initiative_id": str(initiative_id),
        "pipeline_status": "failed",
        "failed_at_slug": body.get("failed_at_slug"),
        "reason": body.get("reason"),
    }


__all__ = ["router"]
