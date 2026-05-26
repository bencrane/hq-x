-- GTM pipeline ledgers (Phase 1).
--
-- Two ledgers backing the Trigger.dev-orchestrated, hq-x-proxied GTM
-- slice-to-campaign pipeline:
--
--   * `ops.task_runs` — generic async-data-operation ledger. One row per
--     Trigger.dev task invocation that crosses the hq-x proxy. Tracks
--     Trigger's `run_id`, the task slug, lifecycle status, input/output
--     cardinalities for cost reconciliation, accumulated cost in cents,
--     and a JSONB error blob populated on failure.
--
--   * `business.gtm_slice_runs` — pipeline state machine for the
--     end-to-end slice → resolve → find-people → validate → campaign
--     flow. One row per pipeline_run_id; `current_step` is the slug of
--     the in-flight step, `total_entities`/`enriched_emails`/
--     `validated_emails` are running counters updated as each step
--     completes.
--
-- Schema choice: `ops.*` for the generic ledger (operational substrate,
-- not domain state), `business.*` for the pipeline state row (couples
-- to audience_specs + downstream campaign artifacts).
--
-- Forward-only migration. IF NOT EXISTS throughout. Re-applies cleanly.

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS business;

-- ── ops.task_runs ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ops.task_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          TEXT        NOT NULL,
    task_type       TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',
    inputs_count    INTEGER     NOT NULL DEFAULT 0,
    outputs_count   INTEGER     NOT NULL DEFAULT 0,
    cost_cents      INTEGER     NOT NULL DEFAULT 0,
    error_log       JSONB       NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_runs_status_chk
        CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);

-- Trigger.dev `run_id` is the natural external key; unique so a retried
-- Trigger task can UPSERT against the same ledger row.
CREATE UNIQUE INDEX IF NOT EXISTS task_runs_run_id_uniq_idx
    ON ops.task_runs (run_id);

-- Per-task-type lifecycle dashboards.
CREATE INDEX IF NOT EXISTS task_runs_task_type_created_at_desc_idx
    ON ops.task_runs (task_type, created_at DESC);

-- In-flight pivot (cheap — most rows terminate).
CREATE INDEX IF NOT EXISTS task_runs_status_inflight_idx
    ON ops.task_runs (status)
    WHERE status IN ('pending', 'running');

-- ── business.gtm_slice_runs ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business.gtm_slice_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id     TEXT        NOT NULL,
    audience_spec_id    TEXT        NOT NULL,
    current_step        TEXT        NULL,
    status              TEXT        NOT NULL DEFAULT 'pending',
    total_entities      INTEGER     NOT NULL DEFAULT 0,
    enriched_emails     INTEGER     NOT NULL DEFAULT 0,
    validated_emails    INTEGER     NOT NULL DEFAULT 0,
    cost_cents          INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT gtm_slice_runs_status_chk
        CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);

-- One ledger row per pipeline_run_id.
CREATE UNIQUE INDEX IF NOT EXISTS gtm_slice_runs_pipeline_run_id_uniq_idx
    ON business.gtm_slice_runs (pipeline_run_id);

-- Per-audience history view.
CREATE INDEX IF NOT EXISTS gtm_slice_runs_audience_spec_id_created_at_desc_idx
    ON business.gtm_slice_runs (audience_spec_id, created_at DESC);

-- In-flight pivot.
CREATE INDEX IF NOT EXISTS gtm_slice_runs_status_inflight_idx
    ON business.gtm_slice_runs (status)
    WHERE status IN ('pending', 'running');
