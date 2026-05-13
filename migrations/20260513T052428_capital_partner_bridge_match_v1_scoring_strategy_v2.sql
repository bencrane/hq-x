-- 2026-05-13 capital_partner_bridge_match_v1 scoring_strategy v2.
--
-- Cycle: scorer-enrichment-borrower-ucc-history (s5).
--
-- REPLACE (not merge) — the existing flat bridge_tier_bonus shape
-- {'gold':0.15,'silver':0.0,'platinum':0.3} conflicts with the v2
-- nested BridgeTierBonusConfig shape during Pydantic deserialization.
-- PR atomicity is load-bearing: s2 (model) + s5 (migration) must merge
-- together (validator KEEP rationale, decomposition-check).
--
-- PRE-state (verbatim from live SELECT 2026-05-13):
--   {"scalar_weight":1.0,"vector_weight":0.5,
--    "bridge_tier_bonus":{"gold":0.15,"silver":0.0,"platinum":0.3},
--    "recency_boost_weight":0.3}
--
-- POST-state (v2 — what this migration writes):
-- {
--   "scalar_weight": 1.0,
--   "vector_weight": 0.5,
--   "recency_boost_weight": 0.3,
--   "bridge_tier_bonus": {
--     "bridge_namespace": "bridges",
--     "bridge_table": "ucc_pdl_lance",
--     "tier_column": "confidence_tier",
--     "bonus_by_tier": {"platinum": 0.30, "gold": 0.15, "silver": 0.05}
--   },
--   "source_profile_dataset": {
--     "namespace": "borrowers",
--     "table": "ucc_profile_lance",
--     "weight_features": {
--       "ucc_filing_count": 0.02,
--       "ucc_active_lien_count": 0.05,
--       "recent_non_bank_lender_count": 0.15
--     }
--   }
-- }
--
-- Rollback: UPDATE-back with PRE-state JSONB above
--   (rollback harness at scorer-enrichment-borrower-ucc-history-rollback.sh automates this).
--
-- Forward-only per apps/hq-x/CLAUDE.md §"Migration filename convention" +
-- apps/data-engine-x/supabase/migrations/README.md §"Policy".
--
-- Idempotent on re-apply: WHERE IS DISTINCT FROM guard makes subsequent
-- applies no-ops when the row already matches the post-state JSONB.
--
-- Applied via: doppler run --project hq-all --config prd -- bash -c
--   'psql "$HQX_DB_URL_DIRECT" -f apps/hq-x/migrations/20260513T052428_capital_partner_bridge_match_v1_scoring_strategy_v2.sql'

UPDATE business.matching_relationships
SET scoring_strategy = '{
      "scalar_weight": 1.0,
      "vector_weight": 0.5,
      "recency_boost_weight": 0.3,
      "bridge_tier_bonus": {
        "bridge_namespace": "bridges",
        "bridge_table": "ucc_pdl_lance",
        "tier_column": "confidence_tier",
        "bonus_by_tier": {"platinum": 0.30, "gold": 0.15, "silver": 0.05}
      },
      "source_profile_dataset": {
        "namespace": "borrowers",
        "table": "ucc_profile_lance",
        "weight_features": {
          "ucc_filing_count": 0.02,
          "ucc_active_lien_count": 0.05,
          "recent_non_bank_lender_count": 0.15
        }
      }
    }'::jsonb
WHERE name = 'capital_partner_bridge_match_v1'
  AND scoring_strategy IS DISTINCT FROM '{
      "scalar_weight": 1.0,
      "vector_weight": 0.5,
      "recency_boost_weight": 0.3,
      "bridge_tier_bonus": {
        "bridge_namespace": "bridges",
        "bridge_table": "ucc_pdl_lance",
        "tier_column": "confidence_tier",
        "bonus_by_tier": {"platinum": 0.30, "gold": 0.15, "silver": 0.05}
      },
      "source_profile_dataset": {
        "namespace": "borrowers",
        "table": "ucc_profile_lance",
        "weight_features": {
          "ucc_filing_count": 0.02,
          "ucc_active_lien_count": 0.05,
          "recent_non_bank_lender_count": 0.15
        }
      }
    }'::jsonb;
