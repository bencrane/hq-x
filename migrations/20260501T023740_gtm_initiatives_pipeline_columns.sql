-- GTM-pipeline foundation §4.5 — extend business.gtm_initiatives with
-- pipeline orchestration columns.
--
-- Distinct from the existing `status` column (high-level lifecycle:
-- draft / strategy_ready / etc.). pipeline_status tracks the post-payment
-- pipeline-internal state, which is orthogonal — an initiative can be
-- 'strategy_ready' from a prior synthesizer run AND have pipeline_status
-- 'running' for the new MAGS pipeline.
--
-- gating_mode='manual' tells the Trigger workflow to wait.forSignal between
-- steps, gating each on operator approval via the admin "advance" button.

ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS gating_mode TEXT NOT NULL DEFAULT 'auto'
        CHECK (gating_mode IN ('auto', 'manual'));

ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS pipeline_status TEXT
        CHECK (pipeline_status IN ('idle', 'running', 'gated', 'completed', 'failed'));

ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS last_pipeline_run_started_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_gtm_pipeline_status
    ON business.gtm_initiatives (pipeline_status)
    WHERE pipeline_status IS NOT NULL;
