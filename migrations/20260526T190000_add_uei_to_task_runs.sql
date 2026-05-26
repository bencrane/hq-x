-- Add per-entity hydration cache surface to ops.task_runs.
--
-- Motivation: the GTM hydration cascade is keyed at UEI grain, but the
-- ledger row only carried Trigger.dev's run_id + lifecycle counters. The
-- DEX-side extraction slice needs to anti-join the next cohort against
-- UEIs that already completed a hydration task, so we hoist the UEI to a
-- top-level indexed column and capture the upstream provider response as
-- a typed JSONB payload for downstream introspection.
--
-- Forward-only. IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout.

ALTER TABLE ops.task_runs
    ADD COLUMN IF NOT EXISTS uei            TEXT  NULL,
    ADD COLUMN IF NOT EXISTS result_payload JSONB NULL;

-- Anti-join surface: NOT IN (SELECT uei FROM ops.task_runs WHERE status = 'completed')
-- against a 250-row Lance cohort. B-tree on the bare column is sufficient;
-- the planner can fold the status='completed' predicate at scan time on
-- the matching task_type partition.
CREATE INDEX IF NOT EXISTS task_runs_uei_idx
    ON ops.task_runs (uei)
    WHERE uei IS NOT NULL;
