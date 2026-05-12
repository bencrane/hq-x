"""Phase 5 matching engine — relationship-typed entity-to-entity matcher.

The engine is configured by `business.matching_relationships` rows. Each row
declares an intent source (paid_specs, preferences, or both), a scoring
strategy, and a surfacing rule. Match objects are first-class with lifecycle.

The scaffold ships placeholder scoring + portal surfacing for one seeded
relationship (`demand_side_fulfillment_paid_spec_v1`). Operator tunes the
weights + adds more relationships post-hoc — no code change needed for
new relationship types, only a row in `business.matching_relationships`.
"""

from app.services.matching_engine.models import (
    Match,
    MatchReasons,
    RelationshipConfig,
    ScoringStrategy,
    Surfacing,
    SurfacingRule,
)

__all__ = [
    "Match",
    "MatchReasons",
    "RelationshipConfig",
    "ScoringStrategy",
    "Surfacing",
    "SurfacingRule",
]
