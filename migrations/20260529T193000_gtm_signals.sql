-- 20260529T193000_gtm_signals.sql
-- GTM signal definition registry (migrated from DEX ops.gtm_signals) +
-- cohort persistence. Config-only: no FK into DEX data. Warehouse SQL compute
-- stays in DEX (/api/internal/signals/compute) + the gtm MCP; hq-x owns
-- "what is a signal" + the resolved cohort.
-- Forward-only, IF NOT EXISTS everywhere (hq-x convention).

CREATE SCHEMA IF NOT EXISTS business;

-- ── Signal registry ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS business.gtm_signals (
    signal_slug      TEXT        PRIMARY KEY,           -- lowercase_snake_case
    display_name     TEXT        NOT NULL DEFAULT '',   -- human label (slug doubled if empty)
    spine_target     TEXT        NOT NULL,              -- gtm-mcp dotted id: <namespace>.<dataset> (_lance optional)
    criteria         JSONB       NOT NULL,              -- generalized criteria spec
    webhook_test_url TEXT        NOT NULL DEFAULT '',
    webhook_prod_url TEXT        NOT NULL DEFAULT '',
    webhook_target   TEXT        NOT NULL DEFAULT 'test'
                                 CHECK (webhook_target IN ('test','prod')),
    is_active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gtm_signals_is_active_idx
    ON business.gtm_signals (is_active) WHERE is_active;

-- ── Cohort header (one row per resolved run) ─────────────────────────────
CREATE TABLE IF NOT EXISTS business.gtm_signal_cohorts (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_slug       TEXT        NOT NULL
                                  REFERENCES business.gtm_signals(signal_slug)
                                  ON DELETE CASCADE,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    criteria_snapshot JSONB       NOT NULL,             -- exact criteria used (re-runnable)
    spine_target      TEXT        NOT NULL,             -- snapshot of dotted id used
    matched_count     INTEGER     NOT NULL,             -- total pre-cap
    member_count      INTEGER     NOT NULL,             -- rows actually persisted (post-cap)
    truncated         BOOLEAN     NOT NULL DEFAULT FALSE,
    source            TEXT        NOT NULL DEFAULT 'cron'
                                  CHECK (source IN ('cron','manual','preview')),
    compute_ms        INTEGER     NULL,                 -- DEX-reported sql_elapsed_ms
    trigger_run_id    TEXT        NULL,                 -- Trigger.dev ctx.run.id when cron-driven
    dispatch          JSONB       NULL,                 -- webhook dispatch result (status/bytes) or NULL
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gtm_signal_cohorts_slug_run_at_desc_idx
    ON business.gtm_signal_cohorts (signal_slug, run_at DESC);

-- ── Cohort members (N per cohort; one resolved entity each) ───────────────
CREATE TABLE IF NOT EXISTS business.gtm_signal_cohort_members (
    cohort_id   UUID    NOT NULL
                        REFERENCES business.gtm_signal_cohorts(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,                       -- preserves order_by sort
    member      JSONB   NOT NULL,                       -- the resolved row (dataset-agnostic)
    PRIMARY KEY (cohort_id, ordinal)
);

COMMENT ON TABLE business.gtm_signals IS
    'GTM signal definitions (migrated from DEX ops.gtm_signals). Generalized '
    'criteria over any Polaris Lance dataset. hq-x owns definition + lifecycle; '
    'SQL compute runs in DEX (/api/internal/signals/compute) + the gtm MCP.';
COMMENT ON COLUMN business.gtm_signals.spine_target IS
    'gtm-mcp dotted identifier <namespace>.<dataset> (_lance suffix optional). '
    'DEX resolves it to the s3 Lance URI the same way the gtm/polaris MCP does.';
COMMENT ON TABLE business.gtm_signal_cohorts IS
    'One row per signal resolution. criteria_snapshot makes the cohort re-runnable.';
