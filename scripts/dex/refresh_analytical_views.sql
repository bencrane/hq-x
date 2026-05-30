-- =============================================================
-- Analytical Materialized View Refresh Script
-- =============================================================
-- Run with: doppler run -p data-engine-x-api -c prd -- psql -f scripts/refresh_analytical_views.sql
--
-- Refresh frequency guide:
--   DAILY (after FMCSA feed ingestion): FMCSA views
--   WEEKLY (or after USASpending backfill): USASpending views
--   WEEKLY: Federal contract leads view
--
-- Dependency order:
--   1. Latest-snapshot MVs first (census, safety, crashes)
--   2. Master carrier view second (depends on step 1)
--   3. Independent views in any order
-- =============================================================

SET statement_timeout = '0';

-- ---- DAILY: FMCSA latest-snapshot views (run after feed ingestion) ----
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_latest_census;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_latest_safety_percentiles;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_crash_counts_12mo;

-- depends on the three above
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_carrier_master;

-- existing FMCSA views (from migrations 036, 037)
-- NOTE: these depend on migrations 036/037 being applied. As of 2026-03-18, the
-- operational reality check notes these migrations have NOT been applied to production.
-- These lines will fail until those migrations are run.
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_authority_grants;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_insurance_cancellations;

-- ---- WEEKLY: USASpending views ----
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_usaspending_contracts_typed;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_usaspending_first_contracts;

-- ---- WEEKLY: Federal contract leads (existing, from migration 034) ----
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_federal_contract_leads;

-- ---- DAILY: FMCSA gap-fill views from migration 042 ----
-- mv_fmcsa_latest_insurance_policies: active insurance posture per docket (~1.4M rows)
-- mv_fmcsa_new_carriers_90d: carriers added in the last 90 days (~16K rows, window advances daily)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_latest_insurance_policies;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_fmcsa_new_carriers_90d;

-- ---- WEEKLY: SAM.gov and SBA analytical views from migration 042 ----
-- Dependency order: typed base views before aggregate/cross-vertical views
-- mv_sam_gov_entities_typed must refresh before mv_sam_usaspending_bridge

-- SAM.gov base view (typed columns, ~867K rows)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sam_gov_entities_typed;

-- SAM.gov aggregates (trivial row counts ~57 and ~480)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sam_gov_entities_by_state;
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sam_gov_entities_by_naics;

-- SBA typed base view (~356K rows)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sba_loans_typed;

-- SBA aggregate (~57 rows)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sba_loans_by_state;

-- Cross-vertical bridge: depends on mv_sam_gov_entities_typed (above) and
-- mv_usaspending_contracts_typed (already refreshed in WEEKLY block above)
-- (~118K rows; hash join of 867K × 14.7M MV — run last; 5–15 min)
REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sam_usaspending_bridge;

RESET statement_timeout;
