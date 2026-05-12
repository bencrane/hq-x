"""Portal surfacing — match shows in the partner's in-platform feed.

Inserts a `business.match_surfacings` row with channel='portal'. The
partner-platform front-end queries the surfacings table to render the feed.
No external side-effect — partners pull from the surfacings table on portal
load.
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
    """Persist the portal surfacing event."""
    surfacing = Surfacing(
        match_id=match.match_id,  # type: ignore[arg-type]  # always set by caller
        channel="portal",
        surface_metadata={
            "intent_kind": match.intent_kind,
            "spec_id": str(intent.get("spec_id")) if intent.get("spec_id") else None,
        },
        outcome="pending",
    )
    surfacing_id = await persist_surfacing(surfacing)
    surfacing.surfacing_id = surfacing_id
    return surfacing
