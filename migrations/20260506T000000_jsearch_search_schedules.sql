-- JSearch search schedules — operator-defined recurring JSearch ingests.
--
-- Owned by hq-x (orchestration). Trigger.dev fires on cadence; the task
-- declared in apps/hq-x/src/trigger/jsearch-scheduled-ingest.ts calls
-- hq-x's /internal/jsearch/run-scheduled-ingest, which then calls DEX's
-- /api/v1/jsearch/ingest. DEX is the dumb executor; this row is the
-- single source of truth for "what query should run on what cadence."
--
-- Two-system invariant on create/delete: row + Trigger.dev schedule
-- entity (trigger_schedule_id). Service rolls back the row if Trigger
-- create fails; tolerates Trigger delete failure on row delete so the
-- operator can always evict locally.
--
-- Cross-DB linkage: last_run_id is a soft pointer to
-- ops.jsearch_search_ingest_runs.run_id (which lives in DEX). No SQL FK
-- across databases — same pattern as elsewhere in this monorepo.

BEGIN;

CREATE TABLE IF NOT EXISTS business.jsearch_search_schedules (
  schedule_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Search params (mirror of DEX /jsearch/ingest body, minus `page` —
  -- scheduled fires always start at page 1).
  query                 text NOT NULL,
  num_pages             integer NOT NULL DEFAULT 1
    CHECK (num_pages BETWEEN 1 AND 20),
  country               text NOT NULL DEFAULT 'us',
  language              text DEFAULT 'en',
  date_posted           text
    CHECK (date_posted IS NULL OR date_posted IN
      ('all', 'today', '3days', 'week', 'month')),
  work_from_home        boolean NOT NULL DEFAULT false,
  employment_types      text,                                   -- CSV
  job_requirements      text,                                   -- CSV
  radius                numeric,
  exclude_job_publishers text,                                  -- CSV

  -- Cron config.
  cron_expr             text NOT NULL,
  timezone              text NOT NULL DEFAULT 'UTC',
  label                 text,

  -- Trigger.dev linkage. UNIQUE because each schedule maps 1:1 to a
  -- Trigger entity.
  trigger_schedule_id   text NOT NULL UNIQUE,

  -- Lifecycle.
  is_active             boolean NOT NULL DEFAULT true,
  last_fired_at         timestamptz,
  last_run_id           uuid,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE business.jsearch_search_schedules IS
  'Operator-defined recurring JSearch ingests. Cron firing managed by '
  'Trigger.dev (apps/hq-x/src/trigger/jsearch-scheduled-ingest); '
  'externalId on the Trigger entity = schedule_id here. Each fire writes '
  'an audit row in DEX ops.jsearch_search_ingest_runs with schedule_id '
  'populated.';

CREATE INDEX IF NOT EXISTS idx_jsearch_schedules_active
  ON business.jsearch_search_schedules (is_active, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jsearch_schedules_last_fired
  ON business.jsearch_search_schedules (last_fired_at DESC NULLS LAST);

COMMIT;
