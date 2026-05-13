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


class BridgeTierBonusConfig(BaseModel):
    """Config for the bridge-tier-bonus scoring term.

    Identifies the Lance bridge dataset to look up the candidate's tier from,
    the column that holds the tier label, and per-tier bonus values.

    Stored as a sub-object inside `ScoringStrategy.bridge_tier_bonus`.
    """

    bridge_namespace: str = Field(description="Lance dataset namespace (e.g. 'bridges').")
    bridge_table: str = Field(description="Lance table name (e.g. 'ucc_pdl_lance').")
    tier_column: str = Field(description="Column on the bridge holding the tier label.")
    bonus_by_tier: dict[str, float] = Field(
        description="Map of tier label → additive bonus weight."
    )


class SourceProfileDatasetConfig(BaseModel):
    """Config for the source-side profile dataset scoring term.

    Points to a Lance derive that holds per-source-entity profile features.
    Consumed by `_compute_source_profile_features` to add a weighted feature
    sum to the score based on the source intent's entity profile.

    Stored as a sub-object inside `ScoringStrategy.source_profile_dataset`.
    """

    namespace: str = Field(description="Lance dataset namespace (e.g. 'borrowers').")
    table: str = Field(description="Lance table name (e.g. 'ucc_profile_lance').")
    weight_features: dict[str, float] = Field(
        description="Map of feature column name → weight."
    )


class ScoringStrategy(BaseModel):
    """Placeholder scoring weights. Operator tunes post-hoc.

    The scaffold uses:
        scalar_term  = scalar_weight × |matched scalar predicates|
        vector_term  = vector_weight × cosine(query_centroid, target_embedding)
        recency_term = recency_boost_weight × 1/(1 + days_since_target_last_update)
        tier_bonus   = bridge_tier_bonus.bonus_by_tier[candidate_tier] (if configured)
        profile_term = source_profile_dataset weighted feature sum (if configured)

    Stored as JSONB in `business.matching_relationships.scoring_strategy`.
    Strict additive: existing rows without the new optional fields deserialize
    unchanged (both new fields default to None).
    """

    scalar_weight: float = Field(default=1.0, description="Weight for scalar-predicate matches.")
    vector_weight: float = Field(default=1.0, description="Weight for cosine similarity.")
    recency_boost_weight: float = Field(
        default=0.2, description="Weight for the freshness-decay term."
    )
    # scorer-enrichment-borrower-ucc-history cycle additions:
    bridge_tier_bonus: BridgeTierBonusConfig | None = Field(
        default=None,
        description=(
            "Optional: look up the candidate's tier from a configured bridge "
            "and apply an additive tier bonus."
        ),
    )
    source_profile_dataset: SourceProfileDatasetConfig | None = Field(
        default=None,
        description=(
            "Optional: look up the source intent's entity profile in a Lance derive "
            "and apply a weighted feature sum term."
        ),
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
    Strict additive: existing match rows without the new optional fields
    deserialize unchanged (new fields default to None).
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
    # scorer-enrichment-borrower-ucc-history cycle additions:
    bridge_tier_bonus: dict | None = Field(
        default=None,
        description=(
            "Bridge tier bonus applied to this match. "
            "Shape: {'tier': str, 'bonus': float}. None if not configured or tier not found."
        ),
    )
    source_profile_features: dict | None = Field(
        default=None,
        description=(
            "Source-side profile features used to compute the profile term. "
            "Shape: {feature_name: feature_value, ...}. None if not configured or no profile row found."
        ),
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
