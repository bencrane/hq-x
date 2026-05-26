-- Two structural extensions to ops.task_runs:
--
-- 1. Widen the status CHECK constraint to admit 'not_found'. The
--    Modal-side hydrator dispatcher (apps/hq-x/app/routers/internal/
--    gtm_pipeline.py::tasks_enrich modal branch) returns HTTP 200 for
--    BOTH "Blitz returned a populated company record" AND "Blitz cleanly
--    returned `found: false`." Collapsing those into status='completed'
--    masks the second class in the ledger (9.2% of run_cmpn69bky001w0jocnixa2hob).
--    Promote the no-match outcome to a first-class terminal status so the
--    DEX cohort anti-join can exclude confirmed misses on the next
--    250-row test slice without re-burning upstream Blitz calls on UEIs
--    we already know don't resolve.
--
-- 2. Surface the Blitz-input parameters (`domain`, `linkedin_url`) as
--    top-level columns. An execution cache must log the parameters that
--    triggered each upstream call so downstream readers can reconstruct
--    "what did we ask Blitz for" without parsing entity_data out of a
--    JSON envelope. Nullable — the linkedin_url is genuinely absent for
--    UEIs the SAM↔PDL bridge couldn't resolve, and the cache should
--    record that absence faithfully.
--
-- Forward-only. IF EXISTS / IF NOT EXISTS / ADD COLUMN IF NOT EXISTS
-- throughout. Re-applies cleanly.

ALTER TABLE ops.task_runs DROP CONSTRAINT IF EXISTS task_runs_status_chk;

ALTER TABLE ops.task_runs ADD CONSTRAINT task_runs_status_chk
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'not_found'));

ALTER TABLE ops.task_runs
    ADD COLUMN IF NOT EXISTS domain       TEXT NULL,
    ADD COLUMN IF NOT EXISTS linkedin_url TEXT NULL;
