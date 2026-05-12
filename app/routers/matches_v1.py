"""Phase 5 matching-engine REST API.

Endpoints:

  GET  /api/v1/matches/by-signing/{signing_id}         — matches for one signed spec
  GET  /api/v1/matches/by-preference/{preference_id}   — matches for one preference (placeholder)
  GET  /api/v1/operator/match-queue                    — operator review queue
  POST /api/v1/matches/{match_id}/transition           — change match status
  POST /api/v1/operator/match-queue/{surfacing_id}/approve  — operator approves cold-email handoff
  POST /api/v1/operator/match-queue/{surfacing_id}/dismiss  — operator dismisses
  POST /api/v1/internal/matching-engine/evaluate-all   — Trigger.dev daily cron entry point

Auth:
  - Public matches/operator routes use `require_flexible_auth` (operator JWT
    or trigger-shared-secret).
  - The internal /api/v1/internal/matching-engine/evaluate-all endpoint uses
    the trigger-secret pattern (per existing internal/exa_websets.py).

X-Data-Lineage is auto-emitted via the existing LineageMiddleware. The engine's
DB reads are recorded into the request-scoped tracker.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.auth.trigger_secret import verify_trigger_secret
from app.db import get_db_connection
from app.services.matching_engine import engine as engine_mod
from app.services.matching_engine import persistence as persist_mod
from app.services.matching_engine.models import MatchStatus

LOG = logging.getLogger(__name__)

# Public-facing routers.
matches_router = APIRouter(prefix="/api/v1/matches", tags=["matches"])
operator_router = APIRouter(prefix="/api/v1/operator", tags=["matches-operator"])
relationships_router = APIRouter(
    prefix="/api/v1/matching-relationships",
    tags=["matching-relationships"],
)

# Internal router — for the Trigger.dev cron.
matching_internal_router = APIRouter(
    prefix="/api/v1/internal/matching-engine",
    tags=["matches-internal"],
)


# ─── response schemas ────────────────────────────────────────────────────


class MatchResponse(BaseModel):
    match_id: UUID
    source_intent_id: UUID
    intent_kind: str
    relationship_id: UUID
    target_entity_ref: str
    target_source_id: UUID | None
    score: float
    match_reasons: dict[str, Any]
    status: str
    identified_at: datetime
    expires_at: datetime | None
    source_freshness: dict[str, Any] | None
    model_config = ConfigDict(extra="forbid")


class SurfacingResponse(BaseModel):
    surfacing_id: UUID
    match_id: UUID
    channel: str
    surfaced_at: datetime
    surface_metadata: dict[str, Any] | None
    outcome: str | None
    model_config = ConfigDict(extra="forbid")


class OperatorQueueEntry(BaseModel):
    surfacing: SurfacingResponse
    match: MatchResponse
    model_config = ConfigDict(extra="forbid")


class TransitionRequest(BaseModel):
    new_status: MatchStatus
    model_config = ConfigDict(extra="forbid")


class RelationshipResponse(BaseModel):
    relationship_id: UUID
    name: str
    description: str | None
    intent_source: str
    enabled: bool
    scoring_strategy: dict[str, Any]
    surfacing_rule: dict[str, Any]
    created_at: datetime
    model_config = ConfigDict(extra="forbid")


class EvaluateAllResponse(BaseModel):
    relationships_evaluated: int
    total_matches: int
    per_relationship: dict[UUID, int]
    model_config = ConfigDict(extra="forbid")


# ─── helpers ─────────────────────────────────────────────────────────────


def _match_row_to_response(row: tuple) -> MatchResponse:
    (
        match_id, source_intent_id, intent_kind, relationship_id,
        target_entity_ref, target_source_id, score, match_reasons,
        status_, identified_at, expires_at, source_freshness,
    ) = row
    return MatchResponse(
        match_id=match_id,
        source_intent_id=source_intent_id,
        intent_kind=intent_kind,
        relationship_id=relationship_id,
        target_entity_ref=target_entity_ref,
        target_source_id=target_source_id,
        score=float(score),
        match_reasons=match_reasons or {},
        status=status_,
        identified_at=identified_at,
        expires_at=expires_at,
        source_freshness=source_freshness,
    )


def _surfacing_row_to_response(row: tuple) -> SurfacingResponse:
    surfacing_id, match_id, channel, surfaced_at, surface_metadata, outcome = row
    return SurfacingResponse(
        surfacing_id=surfacing_id,
        match_id=match_id,
        channel=channel,
        surfaced_at=surfaced_at,
        surface_metadata=surface_metadata,
        outcome=outcome,
    )


_MATCH_SELECT = """
    match_id, source_intent_id, intent_kind, relationship_id,
    target_entity_ref, target_source_id, score, match_reasons,
    status, identified_at, expires_at, source_freshness
"""

_SURFACING_SELECT = """
    surfacing_id, match_id, channel, surfaced_at, surface_metadata, outcome
"""

# Aliased variants for JOIN queries where a bare column name would be
# ambiguous (both `business.match_surfacings` and `business.matches`
# carry `match_id`). The operator queue JOIN reads both tables — without
# explicit aliasing the bare `match_id` column reference fails with
# AmbiguousColumn at first surfacing row.
_MATCH_SELECT_ALIASED_M = ", ".join(
    f"m.{c.strip()}" for c in _MATCH_SELECT.strip().split(",")
)
_SURFACING_SELECT_ALIASED_S = ", ".join(
    f"s.{c.strip()}" for c in _SURFACING_SELECT.strip().split(",")
)


# ─── matches public endpoints ────────────────────────────────────────────


@matches_router.get("/by-signing/{signing_id}", response_model=list[MatchResponse])
async def matches_by_signing(
    signing_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[MatchResponse]:
    """List matches for a signed paid spec.

    Returns [] when nothing has been persisted for this signing yet — the
    scaffold's daily cron is what populates this.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_MATCH_SELECT}
                FROM business.matches
                WHERE source_intent_id = %s AND intent_kind = 'paid_spec'
                ORDER BY score DESC, identified_at DESC
                """,
                (str(signing_id),),
            )
            rows = await cur.fetchall()
    return [_match_row_to_response(r) for r in rows]


@matches_router.get("/by-preference/{preference_id}", response_model=list[MatchResponse])
async def matches_by_preference(
    preference_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[MatchResponse]:
    """List matches for an entity preference.

    Preferences substrate is Phase 5.1 — this returns [] in v1 since there's
    no preference table to read from yet. Endpoint exists so consumers can
    code against the stable surface.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_MATCH_SELECT}
                FROM business.matches
                WHERE source_intent_id = %s AND intent_kind = 'preference'
                ORDER BY score DESC, identified_at DESC
                """,
                (str(preference_id),),
            )
            rows = await cur.fetchall()
    return [_match_row_to_response(r) for r in rows]


@matches_router.get("/{match_id}", response_model=MatchResponse)
async def get_match(
    match_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> MatchResponse:
    """Read a single match by id. Operator UI uses this for match-detail view."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_MATCH_SELECT} FROM business.matches WHERE match_id = %s",
                (str(match_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "match_not_found", "match_id": str(match_id)},
        )
    return _match_row_to_response(row)


@matches_router.get("/{match_id}/surfacings", response_model=list[SurfacingResponse])
async def get_match_surfacings(
    match_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[SurfacingResponse]:
    """Surfacing history for one match (which channels, when, outcomes)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_SURFACING_SELECT}
                FROM business.match_surfacings
                WHERE match_id = %s
                ORDER BY surfaced_at DESC
                """,
                (str(match_id),),
            )
            rows = await cur.fetchall()
    return [_surfacing_row_to_response(r) for r in rows]


@matches_router.post("/{match_id}/transition", response_model=MatchResponse)
async def transition_match_endpoint(
    match_id: UUID,
    body: TransitionRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> MatchResponse:
    """Move a match to a new lifecycle status. Guarded by the transition graph."""
    try:
        await persist_mod.transition_match(match_id, body.new_status)
    except persist_mod.InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"type": "invalid_transition", "message": str(exc)},
        ) from exc
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_MATCH_SELECT} FROM business.matches WHERE match_id = %s",
                (str(match_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "match_not_found", "match_id": str(match_id)},
        )
    return _match_row_to_response(row)


# ─── operator-queue endpoints ────────────────────────────────────────────


@operator_router.get("/match-queue", response_model=list[OperatorQueueEntry])
async def operator_match_queue(
    _auth: FlexibleContext = Depends(require_flexible_auth),
    relationship_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    channel: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[OperatorQueueEntry]:
    """Return matches pending operator approval for cold-email handoff.

    Joins `business.match_surfacings` (channel='operator_queue' OR
    channel='cold_email_handoff' WITH outcome='pending') against
    `business.matches`. Operator UI in hq-command consumes this list.

    Optional filters:
      - relationship_id: UUID — match only this relationship
      - status: comma-separated list of match statuses (overrides default
        which limits to surfacings with outcome='pending')
      - channel: limit to one channel
      - min_score: filter matches with score >= min_score
    """
    where_clauses = ["1=1"]
    params: list[Any] = []

    if status_filter is not None:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join(["%s"] * len(statuses))
            where_clauses.append(f"m.status IN ({placeholders})")
            params.extend(statuses)
        # When a status filter is given the operator may want non-pending
        # surfacings too — skip the default 'pending' constraint.
    else:
        where_clauses.append("s.outcome = 'pending'")

    if channel is not None:
        where_clauses.append("s.channel = %s")
        params.append(channel)
    else:
        where_clauses.append("s.channel IN ('operator_queue', 'cold_email_handoff')")

    if relationship_id is not None:
        where_clauses.append("m.relationship_id = %s")
        params.append(str(relationship_id))

    if min_score is not None:
        where_clauses.append("m.score >= %s")
        params.append(min_score)

    where_sql = " AND ".join(where_clauses)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT
                    {_SURFACING_SELECT_ALIASED_S},
                    {_MATCH_SELECT_ALIASED_M}
                FROM business.match_surfacings s
                JOIN business.matches m ON m.match_id = s.match_id
                WHERE {where_sql}
                ORDER BY m.score DESC, s.surfaced_at DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            rows = await cur.fetchall()
    out: list[OperatorQueueEntry] = []
    for r in rows:
        surfacing_row = r[:6]
        match_row = r[6:]
        out.append(
            OperatorQueueEntry(
                surfacing=_surfacing_row_to_response(surfacing_row),
                match=_match_row_to_response(match_row),
            )
        )
    return out


@operator_router.post(
    "/match-queue/{surfacing_id}/approve",
    response_model=SurfacingResponse,
)
async def approve_surfacing(
    surfacing_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> SurfacingResponse:
    """Flip a pending surfacing's outcome to 'sent'.

    In the scaffold this is a STUB — it updates the DB row only. In the
    production version this is also where the real emailbison webhook fires
    (only for channel='cold_email_handoff').
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.match_surfacings
                SET outcome = 'sent'
                WHERE surfacing_id = %s AND outcome = 'pending'
                RETURNING surfacing_id, match_id, channel, surfaced_at,
                          surface_metadata, outcome
                """,
                (str(surfacing_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "surfacing_not_found_or_not_pending",
                "surfacing_id": str(surfacing_id),
            },
        )
    return _surfacing_row_to_response(row)


@operator_router.post(
    "/match-queue/{surfacing_id}/dismiss",
    response_model=SurfacingResponse,
)
async def dismiss_surfacing(
    surfacing_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> SurfacingResponse:
    """Flip a pending surfacing's outcome to 'dismissed'."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.match_surfacings
                SET outcome = 'dismissed'
                WHERE surfacing_id = %s AND outcome = 'pending'
                RETURNING surfacing_id, match_id, channel, surfaced_at,
                          surface_metadata, outcome
                """,
                (str(surfacing_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "surfacing_not_found_or_not_pending",
                "surfacing_id": str(surfacing_id),
            },
        )
    return _surfacing_row_to_response(row)


# ─── matching-relationships read endpoints ───────────────────────────────


@relationships_router.get("", response_model=list[RelationshipResponse])
async def list_relationships(
    _auth: FlexibleContext = Depends(require_flexible_auth),
    enabled_only: bool = Query(default=False),
) -> list[RelationshipResponse]:
    """List configured matching-relationships. Operator UI uses this for filters."""
    where_sql = "WHERE enabled IS TRUE" if enabled_only else ""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT relationship_id, name, description, intent_source,
                       enabled, scoring_strategy, surfacing_rule, created_at
                FROM business.matching_relationships
                {where_sql}
                ORDER BY name
                """
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    out: list[RelationshipResponse] = []
    for r in rows:
        d = dict(zip(cols, r, strict=True))
        out.append(RelationshipResponse(**d))
    return out


# ─── internal endpoint (Trigger.dev daily cron) ──────────────────────────


@matching_internal_router.post(
    "/evaluate-all",
    response_model=EvaluateAllResponse,
    dependencies=[Depends(verify_trigger_secret)],
)
async def evaluate_all_endpoint() -> EvaluateAllResponse:
    """Daily cron entry point. Evaluates every enabled relationship."""
    result = await engine_mod.evaluate_all_active_relationships()
    per_rel = {rel_id: len(matches) for rel_id, matches in result.items()}
    return EvaluateAllResponse(
        relationships_evaluated=len(result),
        total_matches=sum(per_rel.values()),
        per_relationship=per_rel,
    )
