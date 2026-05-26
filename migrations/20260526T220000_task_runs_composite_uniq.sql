-- Replace the run_id-only unique index on ops.task_runs with a composite
-- (run_id, uei) unique index.
--
-- Motivation: gtm_hydration_cascade_test fans out one POST per cohort entity
-- through /internal/tasks/enrich, but every POST in the fan-out shares the
-- same Trigger.dev root run_id. Under the prior task_runs_run_id_uniq_idx
-- (run_id alone), only the first INSERT in any cohort > 1 row succeeded;
-- subsequent INSERTs tripped the unique constraint. The 2026-05-26
-- run_cmpn5j6wj00070hockxxf6d54 trace shows 1 / 250 succeeded.
--
-- The fix preserves the documented contract that ops.task_runs.run_id is
-- Trigger.dev's root run id (per app.routers.internal.gtm_pipeline.EnrichTaskPayload)
-- by moving the uniqueness boundary to the per-entity grain the proxy
-- already writes (see 20260526T190000_add_uei_to_task_runs.sql which added
-- the uei column for exactly this anti-join surface).
--
-- Forward-only. IF EXISTS / IF NOT EXISTS throughout. Re-applies cleanly.

DROP INDEX IF EXISTS ops.task_runs_run_id_uniq_idx;

CREATE UNIQUE INDEX IF NOT EXISTS task_runs_run_id_uei_uniq_idx
    ON ops.task_runs (run_id, uei);
