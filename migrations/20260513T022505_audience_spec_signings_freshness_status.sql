-- FMCSA pipeline remediation: add freshness_status column to business.audience_spec_signings (s2b)
-- and flag stale TX-FMCSA audience-spec signings (s4 data UPDATE bundled here for atomicity).
--
-- Directive: ~/Desktop/hq/directives/2026-05-12-fmcsa-pipeline-remediation.md
-- Stage: 3.C executor (2026-05-13 UTC)
-- Surfaces: s2b (schema) + s4 (data)
-- Target DB: HQ-X ($HQX_DB_URL_DIRECT)
--
-- CRITICAL: This migration targets the HQ-X Postgres database, NOT DEX.
-- Apply via: cd apps/hq-x && doppler run --project hq-all --config prd -- bash -c 'uv run python -m scripts.migrate'
-- NO auto-apply hook exists for HQ-X migrations (confirmed: apps/hq-x/scripts/git-hooks/ absent).
--
-- freshness_status column semantics:
--   NULL                   — treat as fresh (default; legacy rows)
--   'fresh'                — explicitly approved by operator
--   'stale-do-not-surface' — cohort manifest cached at signing-time has gone stale
--                            (upstream source dormant); operator should not serve
--   'archived'             — soft-retired
--
-- Note: downstream router (apps/hq-x/app/routers/audience_specs_v1.py) does NOT yet
-- filter on freshness_status='stale-do-not-surface'. Data-only flag; consumer-update
-- is a separate follow-up cycle (per scope-decomposer ratification).
--
-- The existing trigger trg_audience_spec_signings_set_expires_at watches only
-- signed_at/contract_term_days; this new column is trigger-safe.
--
-- Forward-only. No paired _down.sql. Idempotent: ADD COLUMN IF NOT EXISTS.

-- s2b: Add freshness_status column
ALTER TABLE business.audience_spec_signings
  ADD COLUMN IF NOT EXISTS freshness_status text;

-- Idempotent CHECK constraint
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_name = 'audience_spec_signings_freshness_status_check'
  ) THEN
    ALTER TABLE business.audience_spec_signings DROP CONSTRAINT audience_spec_signings_freshness_status_check;
  END IF;
  ALTER TABLE business.audience_spec_signings
    ADD CONSTRAINT audience_spec_signings_freshness_status_check
    CHECK (freshness_status IS NULL OR freshness_status IN ('fresh', 'stale-do-not-surface', 'archived'));
END$$;

COMMENT ON COLUMN business.audience_spec_signings.freshness_status IS
  'Operator-managed flag for whether a signing should be surfaced via /api/v1/signings. '
  'NULL = treat as fresh (default; legacy rows). "fresh" = explicitly approved. '
  '"stale-do-not-surface" = cohort manifest cached at signing-time has gone stale '
  '(upstream source dormant); operator should not serve. "archived" = soft-retired. '
  'See ~/Desktop/hq/directives/2026-05-12-fmcsa-pipeline-remediation.md.';

-- s4: Flag 4 TX-FMCSA audience-spec signings as stale-do-not-surface.
-- These 4 signings (12,338 carriers each; PHY_STATE=TX, SAFETY_RATING=S;
-- sources=fmcsa.company_census_file) were signed on 2026-05-12 with static R2
-- cohort manifests. The underlying FMCSA canonical layer has been dormant since
-- 2026-05-07; the signing manifests reference carrier state frozen at 2026-04-25.
-- The operator-judgment call: do not surface these during the 90-day contract term.
-- (Stage 3.A audit verified count_at_signing=12338 anchor matches exactly 4 rows.)
-- (Stage 3.B reviewer re-confirmed count=4 against live HQ-X DB on 2026-05-12.)
UPDATE business.audience_spec_signings
   SET freshness_status = 'stale-do-not-surface'
 WHERE count_at_signing = 12338
   AND signing_id IN (
     '514047e9-7b1a-46be-bf4a-26440d102990',
     '9999b3a8-88b6-4a12-8d2e-8a0c6836cf00',
     'dd0ca151-654f-4cca-9053-473a3a4a0e7a',
     'd3f8fa9f-d22f-452f-b0ff-180d74398600'
   );
