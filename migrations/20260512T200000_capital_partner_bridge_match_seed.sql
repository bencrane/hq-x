-- 2026-05-12 capital_partner_bridge_match_v1 seed.
--
-- Inserts a row into business.matching_relationships for the new
-- relationship that consumes Lance bridges (PDL/SAM/USAspending × SBA
-- borrower) and surfaces candidates against demand-side specs.
--
-- Mirrors the schema of the existing seeded
-- demand_side_fulfillment_paid_spec_v1 row (migration
-- 20260512T040000_matching_engine_substrate.sql) — same target_filter
-- empty-object pattern, same scoring_strategy three-weight shape,
-- same surfacing_rule channel/when/auto_narrative/operator_approval shape.
-- Adds bridge_tier_bonus to scoring_strategy (forward-compatible jsonb field;
-- consumers that haven't implemented tier bonuses ignore it).
--
-- Forward-only per apps/hq-x/CLAUDE.md §"Migration filename convention".
-- ON CONFLICT (name) DO NOTHING for idempotent re-apply.

INSERT INTO business.matching_relationships (
  name, description, intent_source, target_filter,
  scoring_strategy, surfacing_rule, enabled
)
VALUES (
  'capital_partner_bridge_match_v1',
  'SBA borrower in COMMIT or recent-approval status with PDL/SAM/USAspending enrichment, matched against demand-side capital-partner specs that filter on (ticket_size_range, naics, state, lender_type=''non_bank'' OR franchise_brand IN spec_target_brands).',
  'paid_specs',
  '{"require_bridge_enrichment": ["pdl_sba_borrower_lance", "sam_sba_borrower_lance", "usaspending_sba_borrower_lance"], "min_confidence_tier": "gold"}'::jsonb,
  '{"scalar_weight": 1.0, "vector_weight": 0.5, "recency_boost_weight": 0.3, "bridge_tier_bonus": {"platinum": 0.3, "gold": 0.15, "silver": 0.0}}'::jsonb,
  '{"channels": ["portal", "operator_queue"], "when": "on_match", "operator_approval_required": true, "auto_narrative": "optional"}'::jsonb,
  true
)
ON CONFLICT (name) DO NOTHING;
