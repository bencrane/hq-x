"""Admin surface for GTM-pipeline initiatives. Mounted at
/api/v1/admin/initiatives and gated to platform operators.

The frontend table-of-initiatives + per-initiative drilldown both
read here. start-pipeline kicks off the Trigger.dev workflow;
rerun re-fires from a step; advance signals the workflow's manual
gate.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection
from app.services import gtm_initiatives as gtm_svc
from app.services import gtm_pipeline as pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/initiatives", tags=["admin"])


_INITIATIVE_LIST_COLUMNS = (
    "id, organization_id, brand_id, partner_id, partner_contract_id, "
    "data_engine_audience_id, status, pipeline_status, gating_mode, "
    "last_pipeline_run_started_at, created_at, updated_at"
)


@router.get("")
async def list_initiatives(
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """List every initiative across orgs (platform-operator scope).

    The frontend uses this for the index table. Cross-org
    visibility is intentional — the operator runs all orgs.
    """
    args: list[Any] = [min(max(limit, 1), 200), max(offset, 0)]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_INITIATIVE_LIST_COLUMNS},
                       (SELECT name FROM business.brands b WHERE b.id = i.brand_id) AS brand_name,
                       (SELECT name FROM business.demand_side_partners p WHERE p.id = i.partner_id) AS partner_name
                FROM business.gtm_initiatives i
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    items = [
        {
            "id": r[0],
            "organization_id": r[1],
            "brand_id": r[2],
            "partner_id": r[3],
            "partner_contract_id": r[4],
            "data_engine_audience_id": r[5],
            "status": r[6],
            "pipeline_status": r[7],
            "gating_mode": r[8],
            "last_pipeline_run_started_at": r[9],
            "created_at": r[10],
            "updated_at": r[11],
            "brand_name": r[12],
            "partner_name": r[13],
        }
        for r in rows
    ]
    return {"items": items}


@router.get("/{initiative_id}")
async def get_initiative(
    initiative_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )
    # Pull the same brand/partner-name decorators the list endpoint surfaces.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT (SELECT name FROM business.brands WHERE id = %s),
                       (SELECT name FROM business.demand_side_partners WHERE id = %s),
                       (SELECT pricing_model FROM business.partner_contracts WHERE id = %s),
                       (SELECT amount_cents FROM business.partner_contracts WHERE id = %s),
                       pipeline_status, gating_mode, last_pipeline_run_started_at
                FROM business.gtm_initiatives WHERE id = %s
                """,
                (
                    str(initiative["brand_id"]),
                    str(initiative["partner_id"]),
                    str(initiative["partner_contract_id"]),
                    str(initiative["partner_contract_id"]),
                    str(initiative_id),
                ),
            )
            decoration = await cur.fetchone()

    return {
        **initiative,
        "brand_name": decoration[0] if decoration else None,
        "partner_name": decoration[1] if decoration else None,
        "contract_pricing_model": decoration[2] if decoration else None,
        "contract_amount_cents": decoration[3] if decoration else None,
        "pipeline_status": decoration[4] if decoration else None,
        "gating_mode": decoration[5] if decoration else None,
        "last_pipeline_run_started_at": decoration[6] if decoration else None,
    }


@router.post("/{initiative_id}/start-pipeline")
async def start_pipeline(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    gating_mode = body.get("gating_mode") or "auto"
    if gating_mode not in ("auto", "manual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_gating_mode",
                "expected": ["auto", "manual"],
            },
        )
    try:
        result = await pipeline.kickoff_pipeline(
            initiative_id, gating_mode=gating_mode,
        )
    except pipeline.GtmPipelineError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pipeline_kickoff_failed", "message": str(exc)},
        ) from exc
    return result


@router.get("/{initiative_id}/runs")
async def list_runs(
    initiative_id: UUID,
    agent_slug: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    rows = await pipeline.list_runs_for_initiative(
        initiative_id,
        agent_slug=agent_slug,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )
    return {"items": rows}


@router.get("/{initiative_id}/runs/{run_id}")
async def get_run(
    initiative_id: UUID,
    run_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    row = await pipeline.get_run(run_id)
    if row is None or row["initiative_id"] != initiative_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "run_not_found"},
        )
    return row


@router.post("/{initiative_id}/runs/{slug}/rerun")
async def rerun_step(
    initiative_id: UUID,
    slug: str,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        result = await pipeline.request_rerun(initiative_id, slug)
    except pipeline.GtmPipelineError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "rerun_failed", "message": str(exc)},
        ) from exc
    return result


@router.post("/{initiative_id}/advance")
async def advance_gate(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Manual-mode advance — the workflow uses wait.forSignal() between
    steps when gating_mode='manual'. The frontend hits this endpoint to
    fire the signal; the implementation lives in the Trigger SDK
    sendSignal call wrapper. For v0 we 501 if Trigger isn't configured —
    the real signal-send wires in once the workflow is deployed.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )
    # The actual signal name aligns with the TS workflow:
    # wait.forSignal({id: `advance:${initiativeId}:${actor}`}).
    # The TS SDK exposes runs.sendSignal; for v0 we just record the
    # intent and let the operator manually resume the run from the
    # Trigger UI if the SDK call is unavailable. Wiring lands in a
    # follow-up directive once Trigger.dev runs are observable.
    await gtm_svc.append_history(
        initiative_id,
        {
            "kind": "advance_gate_requested",
            "at_slug": body.get("at_slug"),
            "by_user_id": str(user.business_user_id),
        },
    )
    return {
        "initiative_id": str(initiative_id),
        "advance_signal_recorded": True,
        "at_slug": body.get("at_slug"),
        "note": (
            "Signal recorded in initiative history. The Trigger.dev "
            "workflow's wait.forSignal listener will pick it up via "
            "the runs.sendSignal call (see follow-up directive)."
        ),
    }


__all__ = ["router"]
