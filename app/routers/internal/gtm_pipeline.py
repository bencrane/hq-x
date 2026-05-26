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

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.auth.trigger_secret import verify_trigger_secret
from app.db import get_db_connection
from app.services import blitz_client
from app.services import gtm_initiatives as gtm_svc
from app.services import gtm_pipeline as pipeline

logger = logging.getLogger(__name__)

# Phase 1 Bulk Firmographic Hydration — Modal Web Function entrypoint.
# Placeholder URL; swap for the deployed Modal app's stable web URL once the
# DEX-side Modal app exists. Public endpoint, no auth header — matches the
# txdot-letting Modal-from-hq-x precedent.
_MODAL_HYDRATION_URL = (
    "https://bencrane--data-engine-x-gtm-hydration-modal-run.modal.run"
)

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

    Per-entity enrichment envelope. The ``gtm_slice_enrichment``
    Trigger.dev task fans out one POST per entity; the proxy materializes
    a row in ``ops.task_runs`` keyed by ``task_run_id`` (Trigger.dev's
    root run id) with ``task_type = f"{provider}_{action}"``.
    """

    model_config = {"extra": "forbid"}

    task_run_id: str = Field(..., description="Trigger.dev run ID.")
    provider: str = Field(..., description="Upstream enrichment provider slug.")
    action: str = Field(..., description="Enrichment action slug.")
    entity_data: dict[str, Any] = Field(
        ...,
        description="The single entity payload Trigger is dispatching.",
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
    task_type = f"{payload.provider}_{payload.action}"
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO ops.task_runs
                        (run_id, task_type, status, inputs_count)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (payload.task_run_id, task_type, "pending", 1),
                )
            await conn.commit()
    except Exception as exc:
        logger.exception(
            "tasks_enrich_ledger_insert_failed",
            extra={
                "task_run_id": payload.task_run_id,
                "task_type": task_type,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "ledger_insert_failed",
                "message": str(exc),
            },
        ) from exc

    # Provider routing — execute the upstream call and finalize the
    # ledger row. Only blitz is wired in this phase; other providers
    # leave the row at 'pending' for a subsequent phase to drain.
    final_status: str | None = None
    result_payload: dict[str, Any] | None = None
    error_dict: dict[str, Any] | None = None

    if payload.provider == "blitz":
        try:
            result_payload = await blitz_client.call(
                payload.action, payload.entity_data
            )
            final_status = "completed"
        except blitz_client.BlitzCallError as exc:
            final_status = "failed"
            error_dict = {
                "kind": "BlitzCallError",
                "message": str(exc),
                "status_code": exc.status_code,
                "endpoint": exc.endpoint,
            }
        except blitz_client.BlitzError as exc:
            final_status = "failed"
            error_dict = {
                "kind": exc.__class__.__name__,
                "message": str(exc),
            }
        except httpx.HTTPError as exc:
            final_status = "failed"
            error_dict = {
                "kind": exc.__class__.__name__,
                "message": str(exc),
            }

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE ops.task_runs
                        SET status = %s,
                            outputs_count = %s,
                            error_log = %s,
                            updated_at = now()
                        WHERE run_id = %s
                        """,
                        (
                            final_status,
                            1 if final_status == "completed" else 0,
                            Jsonb(error_dict) if error_dict else None,
                            payload.task_run_id,
                        ),
                    )
                await conn.commit()
        except Exception as exc:
            logger.exception(
                "tasks_enrich_ledger_update_failed",
                extra={
                    "task_run_id": payload.task_run_id,
                    "task_type": task_type,
                    "final_status": final_status,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "ledger_update_failed",
                    "message": str(exc),
                },
            ) from exc

    elif payload.provider == "modal":
        # Heavy waterfall dispatcher — POSTs to the GTM-hydration Modal Web
        # Function, captures the response into the ledger row, and surfaces
        # the terminal status back to the Trigger.dev caller so the
        # orchestrator can rely on `ack.status === "completed"` as ground
        # truth (no silent failures).
        #
        # PLACEHOLDER URL: real Modal endpoint to be wired before first prod
        # cohort. Modal Web Function precedent: stable public URL, JSON body,
        # no auth header (see apps/hq-x/src/trigger/txdot-letting-monthly.ts).
        modal_body = {
            "action": payload.action,
            "entity_data": payload.entity_data,
        }
        # 80s budget — bounded ~10s under the Trigger task's 90s `callHqx`
        # client-side timeout so the proxy returns a structured failure ack
        # before the orchestrator sees a severed connection.
        try:
            async with httpx.AsyncClient(timeout=80.0) as client:
                resp = await client.post(_MODAL_HYDRATION_URL, json=modal_body)
            if resp.status_code // 100 == 2:
                try:
                    result_payload = resp.json()
                except ValueError:
                    result_payload = {"raw_response": resp.text}
                # The Modal hydrator returns HTTP 200 even on Blitz-side
                # failures, surfacing the terminal state via
                # `result_payload["status"]`. Inspect it so the ledger row
                # records the truth instead of always landing on 'completed'.
                modal_reported_status = (
                    result_payload.get("status")
                    if isinstance(result_payload, dict)
                    else None
                )
                if modal_reported_status == "failed":
                    final_status = "failed"
                    error_dict = {
                        "kind": "ModalReportedFailure",
                        "message": (
                            result_payload.get("error")
                            if isinstance(result_payload, dict)
                            else None
                        ) or "modal returned status=failed without error detail",
                        "endpoint": _MODAL_HYDRATION_URL,
                    }
                else:
                    final_status = "completed"
            else:
                final_status = "failed"
                error_dict = {
                    "kind": "ModalCallError",
                    "status_code": resp.status_code,
                    "message": resp.text[:500],
                    "endpoint": _MODAL_HYDRATION_URL,
                }
        except httpx.HTTPError as exc:
            final_status = "failed"
            error_dict = {
                "kind": exc.__class__.__name__,
                "message": str(exc),
                "endpoint": _MODAL_HYDRATION_URL,
            }

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE ops.task_runs
                        SET status = %s,
                            outputs_count = %s,
                            error_log = %s,
                            updated_at = now()
                        WHERE run_id = %s
                        """,
                        (
                            final_status,
                            1 if final_status == "completed" else 0,
                            Jsonb(error_dict) if error_dict else None,
                            payload.task_run_id,
                        ),
                    )
                await conn.commit()
        except Exception as exc:
            logger.exception(
                "tasks_enrich_ledger_update_failed",
                extra={
                    "task_run_id": payload.task_run_id,
                    "task_type": task_type,
                    "final_status": final_status,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "ledger_update_failed",
                    "message": str(exc),
                },
            ) from exc

    return {
        "acknowledged": True,
        "endpoint": "tasks.enrich",
        "task_run_id": payload.task_run_id,
        "task_type": task_type,
        "status": final_status,
        "result": result_payload,
        "error": error_dict,
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
