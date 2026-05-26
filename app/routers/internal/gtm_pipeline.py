"""Internal endpoints called by the Trigger.dev gtm-run-initiative-pipeline
task. Bearer-authenticated with TRIGGER_SHARED_SECRET, same pattern as
internal/exa_jobs.py + internal/gtm_initiatives.py.

The single source of truth for an agent invocation is
``POST /run-step``: one HTTP call per agent slug, hq-x blocks for the
full Anthropic round trip, every state mutation lands in the DB before
the response returns. Trigger.dev's TS layer holds zero business state.

This module also exports two Phase-1 proxy routers for the new
slice-to-campaign GTM pipeline:

  * ``tasks_router`` — prefix ``/tasks``. Generic async-task proxies.
  * ``gtm_slice_router`` — prefix ``/gtm-slice``. Slice pipeline step
    proxies (resolve / find-people / validate).

All Phase-1 endpoints are pure receive-and-ack: they authenticate the
Trigger.dev payload, return a 200 OK with the structured ack envelope,
and persist nothing. State writes land in subsequent phases.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.trigger_secret import verify_trigger_secret
from app.db import get_db_connection
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

    recipient_id_raw = body.get("recipient_id")
    step_id_raw = body.get("channel_campaign_step_id")
    try:
        recipient_id = UUID(recipient_id_raw) if recipient_id_raw else None
        channel_campaign_step_id = (
            UUID(step_id_raw) if step_id_raw else None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_uuid_kwarg", "message": str(exc)},
        ) from exc

    try:
        result = await pipeline.run_step(
            initiative_id=initiative_id,
            agent_slug=agent_slug,
            hint=body.get("hint"),
            upstream_outputs=body.get("upstream_outputs"),
            recipient_id=recipient_id,
            channel_campaign_step_id=channel_campaign_step_id,
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


@router.post(
    "/initiatives/{initiative_id}/fanout-targets",
    dependencies=[Depends(verify_trigger_secret)],
)
async def fanout_targets(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Cross-product of (recipient × DM step) for the initiative's most
    recent succeeded materialization. Trigger.dev's parent task calls
    this immediately before fanning out the per-recipient creative
    batchTrigger.

    Body: ``{"agent_slug": "<fanout actor slug>"}`` — currently
    ignored beyond schema-level validation; in v0 every fanout step
    consumes the same (recipient × DM step) target set.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Read the most recent succeeded channel-step-materializer's
            # executed.dm_step_ids — this is the authoritative DM step
            # set for the current materialization.
            await cur.execute(
                """
                SELECT output_blob
                FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                  AND agent_slug = 'gtm-channel-step-materializer'
                  AND status = 'succeeded'
                ORDER BY run_index DESC
                LIMIT 1
                """,
                (str(initiative_id),),
            )
            cs_row = await cur.fetchone()
            if cs_row is None or cs_row[0] is None:
                return {"items": [], "expected_count": 0}

            value = (cs_row[0] or {}).get("value") or {}
            executed = (
                value.get("executed") if isinstance(value, dict) else None
            ) or {}
            dm_step_ids = list(executed.get("dm_step_ids") or [])
            if not dm_step_ids:
                return {"items": [], "expected_count": 0}

            await cur.execute(
                """
                SELECT recipient_id
                FROM business.initiative_recipient_memberships
                WHERE initiative_id = %s AND removed_at IS NULL
                ORDER BY added_at
                """,
                (str(initiative_id),),
            )
            recipient_rows = await cur.fetchall()

    items = [
        {
            "recipient_id": str(r[0]),
            "channel_campaign_step_id": str(s),
        }
        for r in recipient_rows
        for s in dm_step_ids
    ]
    return {"items": items, "expected_count": len(items)}


__all__ = ["router", "tasks_router", "gtm_slice_router"]


# ── Phase-1 slice-to-campaign proxy routers ───────────────────────────────


class EnrichTaskPayload(BaseModel):
    """Body for ``POST /internal/tasks/enrich``.

    Generic enrichment-task envelope. ``task_type`` identifies the
    enricher (e.g. ``firmographic``, ``technographic``); ``provider_set``
    enumerates the upstream providers Trigger has been authorized to
    fan out to.
    """

    model_config = {"extra": "forbid"}

    run_id: str = Field(..., description="Trigger.dev run ID.")
    task_type: str = Field(..., description="Enrichment task slug.")
    audience_spec_id: str = Field(..., description="DEX audience spec ID.")
    provider_set: list[str] = Field(
        default_factory=list,
        description="Upstream enrichment providers in fan-out order.",
    )
    inputs_count: int = Field(
        default=0,
        ge=0,
        description="Number of input entities Trigger is dispatching.",
    )


class GtmSliceResolvePayload(BaseModel):
    """Body for ``POST /internal/gtm-slice/resolve``."""

    model_config = {"extra": "forbid"}

    pipeline_run_id: str = Field(
        ...,
        description="Stable pipeline run identifier (Trigger root run).",
    )
    audience_spec_id: str = Field(..., description="DEX audience spec ID.")
    run_id: str | None = Field(
        default=None,
        description="Trigger.dev step run ID (optional).",
    )


class GtmSliceFindPeoplePayload(BaseModel):
    """Body for ``POST /internal/gtm-slice/find-people``."""

    model_config = {"extra": "forbid"}

    pipeline_run_id: str = Field(...)
    audience_spec_id: str = Field(...)
    provider_set: list[str] = Field(
        default_factory=list,
        description="People-finder providers (leadmagic, parallel, …).",
    )
    run_id: str | None = Field(default=None)


class GtmSliceValidatePayload(BaseModel):
    """Body for ``POST /internal/gtm-slice/validate``."""

    model_config = {"extra": "forbid"}

    pipeline_run_id: str = Field(...)
    audience_spec_id: str = Field(...)
    provider_set: list[str] = Field(
        default_factory=list,
        description="Email-validation providers (millionverifier, …).",
    )
    run_id: str | None = Field(default=None)


tasks_router = APIRouter(prefix="/tasks", tags=["internal"])
gtm_slice_router = APIRouter(prefix="/gtm-slice", tags=["internal"])


@tasks_router.post(
    "/enrich",
    dependencies=[Depends(verify_trigger_secret)],
    status_code=status.HTTP_200_OK,
)
async def tasks_enrich(payload: EnrichTaskPayload) -> dict[str, Any]:
    return {
        "acknowledged": True,
        "endpoint": "tasks.enrich",
        "run_id": payload.run_id,
        "task_type": payload.task_type,
        "audience_spec_id": payload.audience_spec_id,
    }


@gtm_slice_router.post(
    "/resolve",
    dependencies=[Depends(verify_trigger_secret)],
    status_code=status.HTTP_200_OK,
)
async def gtm_slice_resolve(payload: GtmSliceResolvePayload) -> dict[str, Any]:
    return {
        "acknowledged": True,
        "endpoint": "gtm-slice.resolve",
        "pipeline_run_id": payload.pipeline_run_id,
        "audience_spec_id": payload.audience_spec_id,
    }


@gtm_slice_router.post(
    "/find-people",
    dependencies=[Depends(verify_trigger_secret)],
    status_code=status.HTTP_200_OK,
)
async def gtm_slice_find_people(
    payload: GtmSliceFindPeoplePayload,
) -> dict[str, Any]:
    return {
        "acknowledged": True,
        "endpoint": "gtm-slice.find-people",
        "pipeline_run_id": payload.pipeline_run_id,
        "audience_spec_id": payload.audience_spec_id,
    }


@gtm_slice_router.post(
    "/validate",
    dependencies=[Depends(verify_trigger_secret)],
    status_code=status.HTTP_200_OK,
)
async def gtm_slice_validate(
    payload: GtmSliceValidatePayload,
) -> dict[str, Any]:
    return {
        "acknowledged": True,
        "endpoint": "gtm-slice.validate",
        "pipeline_run_id": payload.pipeline_run_id,
        "audience_spec_id": payload.audience_spec_id,
    }
