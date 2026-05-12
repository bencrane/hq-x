"""Pydantic models for the matching engine.

These mirror the `business.matching_relationships`, `business.matches`, and
`business.match_surfacings` schema. Kept deliberately simple — the scaffold
exists to wire the engine end-to-end; operator tunes scoring + relationships
post-hoc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Type aliases for clarity.
IntentKind = Literal["paid_spec", "preference"]
IntentSource = Literal["paid_specs", "preferences", "both"]
MatchStatus = Literal[
    "identified", "surfaced", "viewed", "reserved", "claimed", "dismissed", "expired"
]
SurfacingChannel = Literal["portal", "operator_queue", "cold_email_handoff"]
SurfacingOutcome = Literal[
    "pending", "partner_viewed", "partner_acted", "dismissed",
    "sent", "delivered", "responded", "no_response",
]


class ScoringStrategy(BaseModel):
    """Placeholder scoring weights. Operator tunes post-hoc.

    The scaffold uses:
        scalar_weight × |matched scalar predicates|
        + vector_weight × cosine(query_centroid, target_embedding)
        + recency_boost_weight × 1/(1 + days_since_target_last_update)

    Stored as JSONB in `business.matching_relationships.scoring_strategy`.
    """

    scalar_weight: float = Field(default=1.0, description="Weight for scalar-predicate matches.")
    vector_weight: float = Field(default=1.0, description="Weight for cosine similarity.")
    recency_boost_weight: float = Field(
        default=0.2, description="Weight for the freshness-decay term."
    )


class SurfacingRule(BaseModel):
    """Per-relationship surfacing configuration.

    Stored as JSONB in `business.matching_relationships.surfacing_rule`.
    """

    channels: list[SurfacingChannel] = Field(
        default_factory=lambda: ["portal"],
        description="Channels to surface matches on.",
    )
    when: Literal["on_match", "daily_digest", "on_material_change"] = Field(
        default="on_match",
        description="When to fire the surfacing — at identification, in a digest, or on drift.",
    )
    operator_approval_required: bool = Field(
        default=False,
        description=(
            "If true, cold-email handoffs (and any channel that supports it) "
            "wait for operator approval before send. Match_surfacings.outcome "
            "= 'pending' until approved."
        ),
    )
    auto_narrative: Literal["required", "optional", "none"] = Field(
        default="optional",
        description="LLM-generated narrative requirement. v1 scaffold uses placeholder text.",
    )


class RelationshipConfig(BaseModel):
    """A single row from `business.matching_relationships`."""

    relationship_id: UUID
    name: str
    description: str | None = None
    intent_source: IntentSource
    target_filter: dict[str, Any] = Field(default_factory=dict)
    scoring_strategy: ScoringStrategy
    surfacing_rule: SurfacingRule
    enabled: bool = True
    created_at: datetime | None = None
    created_by_user_id: UUID | None = None


class MatchReasons(BaseModel):
    """Structured match-reasons payload. JSONB-serializable.

    Stored as JSONB in `business.matches.match_reasons`.
    """

    scalar_hits: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-attribute hits. Each entry: "
            "{'attribute': str, 'value': Any, 'matched': bool}."
        ),
    )
    vector_similarity: float | None = Field(
        default=None,
        description="Cosine similarity to the query centroid / seed mean. [0, 1] if present.",
    )
    recency_score: float | None = Field(
        default=None,
        description="Freshness term. 1.0 = just-updated, decays toward 0.",
    )
    reranker_score: float | None = Field(
        default=None,
        description="Reserved for a future re-ranker pass. None in v1.",
    )


class Match(BaseModel):
    """A row in `business.matches`.

    `match_id` is None on a freshly-constructed Match; the persistence layer
    fills it on insert/upsert.
    """

    match_id: UUID | None = None
    source_intent_id: UUID
    intent_kind: IntentKind
    relationship_id: UUID
    target_entity_ref: str
    target_source_id: UUID | None = None
    score: float
    match_reasons: MatchReasons
    status: MatchStatus = "identified"
    identified_at: datetime | None = None
    expires_at: datetime | None = None
    source_freshness: dict[str, Any] | None = None


class Surfacing(BaseModel):
    """A row in `business.match_surfacings`."""

    surfacing_id: UUID | None = None
    match_id: UUID
    channel: SurfacingChannel
    surfaced_at: datetime | None = None
    surface_metadata: dict[str, Any] | None = None
    outcome: SurfacingOutcome | None = None
