-- ops.scheduled_tasks — operator control plane + source-of-truth registry for
-- every Trigger.dev scheduled task in the hq-x project (proj_khmvxxrpyloqmnivdetu).
--
-- WHY THIS EXISTS
-- The 2026-05-30 modal.Cron -> Trigger.dev migration moved ~89 cron schedules
-- onto Trigger.dev. They are DECLARATIVE schedules (cron-in-code), which means
-- Trigger.dev's management API CANNOT activate/deactivate them (only IMPERATIVE
-- schedules created via schedules.create() can be toggled). So the operator had
-- no way to (a) confirm a schedule actually fired, or (b) disable one without a
-- code deploy. This table is both halves of the fix:
--
--   1. SOURCE OF TRUTH — the canonical cadence + human description + the REAL
--      work each task does (the Modal app::function it dispatches, or the hq-x
--      endpoint it drives). The status engine (app/services/scheduled_tasks.py)
--      computes the expected last-fire from `cron` and compares it against the
--      actual last Trigger.dev run to derive green / red / grey / amber.
--
--   2. OPERATOR GATE — `is_enabled` is the kill switch. Every scheduled task
--      asks hq-x "am I enabled?" (POST /internal/scheduled-tasks/gate) before
--      doing its work; a disabled task logs + skips its Modal dispatch / hq-x
--      call. Generalizes the per-task DMAAS_RECONCILE_*_ENABLED Doppler flags
--      into one DB-backed switch the operator flips from the hq-zone UI. The
--      gate is FAIL-OPEN: an hq-x blip never silently halts an SLA-critical run.
--
-- execution_kind distinguishes the two runtimes the operator must reason about:
--   'modal_dispatch' — Trigger.dev fires the cron and POSTs Modal; ALL compute
--                      runs in Modal. A green Trigger run only proves the handoff
--                      succeeded, NOT that the Modal job completed (layer 2).
--   'hqx_compute'    — Trigger.dev fires the cron and calls hq-x, which does the
--                      work in-process. A green run means the work actually ran.
--
-- Schema choice: ops.* matches ops.task_runs (the existing run ledger). Not
-- business.* (domain rows) or dmaas.*.
--
-- Forward-only + idempotent per hq-x convention (IF NOT EXISTS everywhere).
-- Row data is seeded out-of-band by scripts/seed_scheduled_tasks.py so the
-- 89-row manifest stays a single editable source rather than frozen in SQL.

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.scheduled_tasks (
    -- Trigger.dev schedule id == taskIdentifier on its runs (e.g. "sec-edgar-form-8k.scan").
    task_id            TEXT        PRIMARY KEY,
    label              TEXT        NOT NULL,
    description        TEXT        NOT NULL DEFAULT '',

    -- Grouping + operator filtering.
    category           TEXT        NOT NULL,
    -- 1 = P1 (client-SLA-critical), 2 = P2 (normal), 3 = P3 (low / infra).
    priority           SMALLINT    NOT NULL DEFAULT 2,
    is_sla_critical    BOOLEAN     NOT NULL DEFAULT false,

    -- Source-of-truth cadence. `cron` MUST mirror the deployed task's cron or the
    -- status engine computes the wrong "expected fire". Re-sync via the seed script.
    cron               TEXT        NOT NULL,
    cron_human         TEXT        NOT NULL DEFAULT '',
    timezone           TEXT        NOT NULL DEFAULT 'UTC',

    -- What the task actually does.
    execution_kind     TEXT        NOT NULL
        CHECK (execution_kind IN ('modal_dispatch', 'hqx_compute')),
    -- For modal_dispatch: the Modal app + function the cron spawns. NULL for hqx_compute.
    modal_app          TEXT,
    modal_function     TEXT,
    -- For hqx_compute: the hq-x endpoint the task drives. NULL for modal_dispatch.
    hqx_endpoint       TEXT,
    -- Operator-facing description of the artifact/effect the work produces.
    produces           TEXT,

    -- Minutes after the expected fire before a missing run flips red. Sized per
    -- cadence by the seed script (sub-hourly small, monthly large).
    grace_minutes      INTEGER     NOT NULL DEFAULT 240,

    -- THE GATE. When false, the task self-skips its work on the next fire.
    is_enabled         BOOLEAN     NOT NULL DEFAULT true,
    disabled_at        TIMESTAMPTZ,
    disabled_by        TEXT,
    disable_reason     TEXT,

    -- Independent fire ledger: stamped every time a fire calls the gate, so hq-x
    -- has its own witness of "this schedule is alive" beyond Trigger.dev's word.
    last_gate_check_at TIMESTAMPTZ,
    last_gate_decision BOOLEAN,

    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Operator filters: by category, by priority, and a fast "what's disabled" pivot.
CREATE INDEX IF NOT EXISTS scheduled_tasks_category_idx
    ON ops.scheduled_tasks (category);
CREATE INDEX IF NOT EXISTS scheduled_tasks_priority_idx
    ON ops.scheduled_tasks (priority);
-- Partial index: disabled rows are the rare exception worth pivoting on cheaply.
CREATE INDEX IF NOT EXISTS scheduled_tasks_disabled_idx
    ON ops.scheduled_tasks (task_id)
    WHERE is_enabled = false;
-- SLA-critical pivot for the dashboard card's "any P1 red?" check.
CREATE INDEX IF NOT EXISTS scheduled_tasks_sla_critical_idx
    ON ops.scheduled_tasks (task_id)
    WHERE is_sla_critical = true;

COMMENT ON TABLE ops.scheduled_tasks IS
    'Operator control plane + source-of-truth registry for hq-x Trigger.dev scheduled tasks. is_enabled is the fail-open gate; cron is the cadence the status engine grades runs against.';
COMMENT ON COLUMN ops.scheduled_tasks.execution_kind IS
    'modal_dispatch = Trigger fires cron + POSTs Modal (compute in Modal; green = handoff ok only). hqx_compute = Trigger calls hq-x which does the work (green = work ran).';
COMMENT ON COLUMN ops.scheduled_tasks.is_enabled IS
    'Operator kill switch. Tasks call POST /internal/scheduled-tasks/gate before working and skip when false. Fail-open: gate errors default to running.';
COMMENT ON COLUMN ops.scheduled_tasks.cron IS
    'Source-of-truth 5-field cron. MUST mirror the deployed task cron; the status engine grades actual runs against the fire times this implies.';
