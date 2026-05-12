"""Phase 3 — cohort-drift REST API.

Public endpoints (operator-facing, FlexibleAuth):

  GET /api/v1/signings/{signing_id}/drift
       List `attribute_changed` deliveries for a signing — the operator
       review queue for "this match is now wrong, decide if you want to
       invalidate."

  GET /api/v1/cohort-drift/recent
       Recent material-change-driven deliveries across all active
       signings — operator dashboard view.

Internal endpoint (Modal cron, FlexibleAuth via TRIGGER_SHARED_SECRET):

  POST /api/v1/internal/cohort-drift/run-cycle
       Trigger one cycle of the cohort-drift scanner. Pulls new
       material_change_events from DEX, scans active signings, emits
       'attribute_changed' deliveries + Telegram alerts.

Per outbound_is_emailbison_intros_are_on_platform.md: the drift surface
is in-platform — never cold-email. Per
operator_data_anxieties_phase_0.md concern #3: this is the load-bearing
"trust-contract" surface; staleness in matches is the existential risk.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.services.cohort_drift_scanner import run_scan_cycle

cohort_drift_router = APIRouter(
    prefix="/api/v1/cohort-drift",
    tags=["cohort-drift"],
)
cohort_drift_internal_router = APIRouter(
    prefix="/api/v1/internal/cohort-drift",
    tags=["cohort-drift"],
)
signings_drift_router = APIRouter(
    prefix="/api/v1/signings",
    tags=["cohort-drift"],
)


# ─── response schemas ────────────────────────────────────────────────


class DriftDeliveryRow(BaseModel):
    """One `attribute_changed` delivery row.

    `attribute_snapshot` carries the per-event old/new value + change
    kind; `metadata` carries the material_change_event_id back-reference.
    """

    delivery_id: UUID
    signing_id: UUID
    entity_ref: str
    event_kind: str
    occurred_at: datetime
    channel: str | None
    attribute_snapshot: dict[str, Any] | None
    metadata: dict[str, Any] | None
    model_config = ConfigDict(extra="forbid")


class RecentDriftRow(BaseModel):
    """One drift row enriched with signing context, for the operator dashboard."""

    delivery_id: UUID
    signing_id: UUID
    spec_id: UUID
    partner_id: UUID
    partner_name: str | None
    entity_ref: str
    occurred_at: datetime
    signed_at: datetime
    count_at_signing: int
    expires_at: datetime
    attribute_snapshot: dict[str, Any] | None
    metadata: dict[str, Any] | None
    model_config = ConfigDict(extra="forbid")


class RunCycleResponse(BaseModel):
    events_scanned: int
    deliveries_inserted: int
    signings_affected: int
    high_water_mark: str | None
    ts: str
    model_config = ConfigDict(extra="forbid")


# ─── endpoints ───────────────────────────────────────────────────────


@signings_drift_router.get(
    "/{signing_id}/drift",
    response_model=list[DriftDeliveryRow],
)
async def list_drift_for_signing(
    signing_id: UUID,
    limit: int = Query(default=200, ge=1, le=2000),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List `attribute_changed` deliveries for one signing.

    The operator's review queue for that signing — every detected material
    change that affected an entity in the cohort manifest.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # 404 if signing doesn't exist (vs empty list for "no drift").
            await cur.execute(
                "SELECT 1 FROM business.audience_spec_signings WHERE signing_id = %s",
                (str(signing_id),),
            )
            if await cur.fetchone() is None:
                raise HTTPException(404, detail={"error": "signing_not_found"})

            await cur.execute(
                """
                SELECT delivery_id, signing_id, entity_ref, event_kind,
                       occurred_at, channel, attribute_snapshot, metadata
                  FROM business.audience_spec_deliveries
                 WHERE signing_id = %s
                   AND event_kind = 'attribute_changed'
                 ORDER BY occurred_at DESC
                 LIMIT %s
                """,
                (str(signing_id), limit),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


@cohort_drift_router.get(
    "/recent",
    response_model=list[RecentDriftRow],
)
async def list_recent_drift(
    limit: int = Query(default=100, ge=1, le=1000),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """Recent `attribute_changed` deliveries across all active signings.

    Joins to audience_spec_signings + audience_specs + organizations so
    the operator dashboard can render partner_name + signing context per
    row without N+1 queries.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.delivery_id,
                       d.signing_id,
                       sp.spec_id,
                       sp.partner_id,
                       o.name             AS partner_name,
                       d.entity_ref,
                       d.occurred_at,
                       s.signed_at,
                       s.count_at_signing,
                       s.expires_at,
                       d.attribute_snapshot,
                       d.metadata
                  FROM business.audience_spec_deliveries d
                  JOIN business.audience_spec_signings s ON s.signing_id = d.signing_id
                  JOIN business.audience_specs sp ON sp.spec_id = s.spec_id
                  LEFT JOIN business.organizations o ON o.id = sp.partner_id
                 WHERE d.event_kind = 'attribute_changed'
                   AND s.expires_at > NOW()
                 ORDER BY d.occurred_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


@cohort_drift_internal_router.post(
    "/run-cycle",
    response_model=RunCycleResponse,
)
async def run_cohort_drift_cycle(
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> RunCycleResponse:
    """Run one cycle of the cohort-drift scanner. Idempotent.

    Pulls new material_change_events from DEX since the persisted high-
    water-mark, scans active signings, emits 'attribute_changed'
    deliveries + Telegram alerts. Returns the summary.

    Called by the Modal cron at modal/material_change_detection_app.py
    every 6 hours, paired with the DEX detection cycle.
    """
    summary = await run_scan_cycle()
    return RunCycleResponse(
        events_scanned=summary["events_scanned"],
        deliveries_inserted=summary["deliveries_inserted"],
        signings_affected=summary["signings_affected"],
        high_water_mark=summary.get("high_water_mark"),
        ts=summary["ts"],
    )
