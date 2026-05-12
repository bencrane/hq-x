"""Operator-queue surfacing — match shows in the operator's review dashboard.

Inserts a `business.match_surfacings` row with channel='operator_queue'. The
operator UI in hq-command (Phase 5.x — REST endpoints exist; UI is follow-up)
queries this channel via `GET /api/v1/operator/match-queue` and acts on the
queue via the transition / approve / dismiss endpoints.
"""

from __future__ import annotations

from typing import Any

from app.services.matching_engine.models import Match, Surfacing, SurfacingRule
from app.services.matching_engine.persistence import persist_surfacing


async def surface_match(
    match: Match,
    rule: SurfacingRule,
    intent: dict[str, Any],
) -> Surfacing:
    """Persist the operator-queue surfacing event."""
    surfacing = Surfacing(
        match_id=match.match_id,  # type: ignore[arg-type]
        channel="operator_queue",
        surface_metadata={
            "intent_kind": match.intent_kind,
            "spec_id": str(intent.get("spec_id")) if intent.get("spec_id") else None,
            "queue_position": None,  # operator UI assigns at render time
        },
        outcome="pending",
    )
    surfacing_id = await persist_surfacing(surfacing)
    surfacing.surfacing_id = surfacing_id
    return surfacing
