-- Phase 5 matching-engine substrate.
--
-- Relationship-typed matching engine. Generic entity-to-entity scoring service
-- configured by relationship rows. No fixed "supply/demand" sides — side is a
-- function of the relationship match, not an entity property.
--
-- Tables:
--   business.matching_relationships  — relationship configs (intent_source, target_filter, scoring, surfacing)
--   business.matches                 — first-class match objects with lifecycle
--   business.match_surfacings        — per-channel surfacing events for a match
--
-- This migration is forward-only. CREATE statements use IF NOT EXISTS so a
-- re-apply after `git revert` is idempotent.
--
-- The `business` schema is created here too: Phase 2 + Phase 3 schema was not
-- applied to prod HQ-X DB at the time this lands (see directive Validator
-- notes). Phase 5 is self-sufficient — no FKs to Phase 2 tables. Recovering
-- Phase 2 + Phase 3 schema in prod is a follow-up cycle.

CREATE SCHEMA IF NOT EXISTS business;

-- ────────────────────── matching_relationships (config) ─────────────────

CREATE TABLE IF NOT EXISTS business.matching_relationships (
    relationship_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Human-readable unique name. The seeded relationship below is
    -- 'demand_side_fulfillment_paid_spec_v1' — the default for paid signed
    -- audience specs that surface in the partner's portal.
    name TEXT NOT NULL UNIQUE,

    description TEXT,

    -- Where the intent comes from. paid_specs reads business.audience_spec_signings;
    -- preferences reads business.entity_preferences (Phase 5.1 — placeholder enum);
    -- both means the engine evaluates against the union.
    intent_source TEXT NOT NULL CHECK (intent_source IN ('paid_specs','preferences','both')),

    -- Optional pre-filter applied to the target population before scoring.
    -- For paid_specs intent, the spec's own sources/filters are the primary
    -- filter; target_filter overlays on top (e.g., "exclude lapsed partners").
    -- For preferences intent, target_filter IS the primary filter.
    target_filter JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Placeholder scoring shape for the scaffold. Operator tunes post-hoc.
    --   {"scalar_weight": float, "vector_weight": float, "recency_boost_weight": float}
    scoring_strategy JSONB NOT NULL,

    -- Where + how matches surface.
    --   {"channels": ["portal" | "operator_queue" | "cold_email_handoff"],
    --    "when": "on_match" | "daily_digest" | "on_material_change",
    --    "operator_approval_required": bool,
    --    "auto_narrative": "required" | "optional" | "none"}
    surfacing_rule JSONB NOT NULL,

    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id UUID
);

CREATE INDEX IF NOT EXISTS idx_matching_relationships_enabled
    ON business.matching_relationships (enabled) WHERE enabled IS TRUE;

-- ────────────────────── matches (first-class match objects) ─────────────

CREATE TABLE IF NOT EXISTS business.matches (
    match_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Polymorphic intent reference. intent_kind distinguishes which table this
    -- points into. No hard FK because the source table varies (audience_spec_signings
    -- vs entity_preferences) and we don't want to force eager Phase 2/preference
    -- substrate existence on the matching engine.
    source_intent_id UUID NOT NULL,
    intent_kind TEXT NOT NULL CHECK (intent_kind IN ('paid_spec','preference')),

    relationship_id UUID NOT NULL REFERENCES business.matching_relationships(relationship_id) ON DELETE RESTRICT,

    -- Canonical entity ID for the matched target. DOT/UEI/EIN/LEI/CRD/duns/etc.
    -- Stored as TEXT so multi-key matches can write the resolved canonical form
    -- without a JOIN to the entity table at query time.
    target_entity_ref TEXT NOT NULL,

    -- The dataset (ops.data_sources row) the target was pulled from. Nullable
    -- because the engine may operate against in-memory cohort manifests (the
    -- signing manifest parquet in R2). No FK — ops.data_sources lives in DEX
    -- DB, this row in HQ-X DB.
    target_source_id UUID,

    -- Score with two-decimal precision. Range [0, +infty) — placeholder weights
    -- in v1 mean scores up to ~3.0; operator tunes the strategy later.
    score NUMERIC(10, 4) NOT NULL,

    -- Structured match reasons:
    --   {"scalar_hits": [{"attribute": "state", "value": "TX", "matched": true}, ...],
    --    "vector_similarity": 0.83,
    --    "recency_score": 0.95,
    --    "reranker_score": null  // reserved for future re-ranker pass}
    match_reasons JSONB NOT NULL,

    -- Match lifecycle. identified = just persisted; surfaced = a surfacing row
    -- exists; viewed/reserved/claimed = partner-side actions; dismissed/expired
    -- terminate without conversion.
    status TEXT NOT NULL CHECK (status IN (
        'identified','surfaced','viewed','reserved','claimed','dismissed','expired'
    )),

    identified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Optional expiry. Cron uses this to flip stale 'identified' matches to
    -- 'expired'. Null = no expiry (relationship config decides).
    expires_at TIMESTAMPTZ,

    -- Per-source freshness for the sources that contributed to the match.
    --   {"<source_qualified_name>": "<ISO8601 last_verified_at>", ...}
    source_freshness JSONB
);

CREATE INDEX IF NOT EXISTS idx_matches_intent
    ON business.matches (source_intent_id, intent_kind);
CREATE INDEX IF NOT EXISTS idx_matches_entity
    ON business.matches (target_entity_ref);
CREATE INDEX IF NOT EXISTS idx_matches_status_score
    ON business.matches (status, score DESC);
CREATE INDEX IF NOT EXISTS idx_matches_relationship
    ON business.matches (relationship_id);

-- Idempotency support: the engine's persist_match() upserts on
-- (source_intent_id, intent_kind, relationship_id, target_entity_ref) within
-- a 24h dedup window. A unique constraint here would be too strict (operator
-- may want re-scoring across runs); the upsert key is enforced in code.

-- ────────────────────── match_surfacings (per-channel events) ───────────

CREATE TABLE IF NOT EXISTS business.match_surfacings (
    surfacing_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id UUID NOT NULL REFERENCES business.matches(match_id) ON DELETE CASCADE,

    -- The channel the match was surfaced on. portal = in-platform partner feed;
    -- operator_queue = operator dashboard for triage; cold_email_handoff =
    -- emailbison webhook (STUB in scaffold — payload logged, outcome='pending').
    channel TEXT NOT NULL CHECK (channel IN (
        'portal','operator_queue','cold_email_handoff'
    )),

    surfaced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Channel-specific metadata.
    --   portal: {"portal_session_id": uuid_or_null}
    --   operator_queue: {"operator_user_id": uuid_or_null, "queue_position": int}
    --   cold_email_handoff: {"emailbison_payload": {...}, "auto_narrative_text": "..."}
    surface_metadata JSONB,

    -- Outcome of the surfacing. pending = awaiting action; partner_* / sent /
    -- delivered / responded / no_response capture downstream signal.
    outcome TEXT CHECK (outcome IN (
        'pending','partner_viewed','partner_acted','dismissed',
        'sent','delivered','responded','no_response'
    ))
);

CREATE INDEX IF NOT EXISTS idx_surfacings_match
    ON business.match_surfacings (match_id);
CREATE INDEX IF NOT EXISTS idx_surfacings_channel_outcome
    ON business.match_surfacings (channel, outcome);

-- ────────────────────── seed: demand_side_fulfillment_paid_spec_v1 ──────
-- The default relationship for paid signed audience specs. Surfaces matches in
-- the partner's portal. Operator tunes the scoring strategy and surfacing rule
-- post-hoc; the scaffold uses placeholder weights so end-to-end wiring is
-- exercised without committing to specific tuning choices.

INSERT INTO business.matching_relationships (
    name, description, intent_source, target_filter,
    scoring_strategy, surfacing_rule, enabled
)
VALUES (
    'demand_side_fulfillment_paid_spec_v1',
    'Default matching for paid audience specs against their declared target population. Placeholder scoring; operator tunes later.',
    'paid_specs',
    '{}'::jsonb,
    '{"scalar_weight": 1.0, "vector_weight": 1.0, "recency_boost_weight": 0.2}'::jsonb,
    '{"channels": ["portal"], "when": "on_match", "operator_approval_required": false, "auto_narrative": "optional"}'::jsonb,
    true
)
ON CONFLICT (name) DO NOTHING;
