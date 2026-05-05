-- business.exa_webset_jobs — orchestration row for every async Exa Websets run.
-- Mirrors business.exa_research_jobs in shape (status enum, JSONB error/history,
-- trigger_run_id, idempotency uniqueness) but has websets-specific columns
-- (count, criteria, enrichments, dex_run_id). The dex_run_id is the UUID that
-- was passed to Exa as external_id and lives in exa.exa_websets on the DEX side.

CREATE TABLE IF NOT EXISTS business.exa_webset_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    created_by_user_id  UUID REFERENCES business.users(id) ON DELETE SET NULL,

    -- The dex_run_id is the id of the exa.exa_websets row in DEX.
    -- It is generated here and passed to DEX when the job is created.
    dex_run_id          UUID NOT NULL DEFAULT gen_random_uuid(),

    -- Webset request parameters (validated server-side before enqueue).
    description         TEXT NOT NULL,
    count               INT NOT NULL,
    criteria            JSONB NOT NULL,   -- list of {type, value} objects
    enrichments         JSONB,            -- list of enrichment column objects (nullable)
    entity              TEXT NOT NULL DEFAULT 'company',

    -- Lifecycle status: queued | running | succeeded | failed
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed'
    )),

    -- Exa-side webset id (populated after the DEX API call creates the webset).
    exa_webset_id       TEXT,

    -- Result summary (populated on success).
    result_summary      JSONB,

    -- Error payload when status=failed.
    error               JSONB,

    -- Audit log of status transitions.
    history             JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Trigger.dev run id that owns this job.
    trigger_run_id      TEXT,

    -- Optional caller-supplied idempotency key.
    idempotency_key     TEXT,

    attempts            INT NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ewj_org_idempotency
    ON business.exa_webset_jobs (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_ewj_status
    ON business.exa_webset_jobs (status);

CREATE INDEX IF NOT EXISTS ix_ewj_org_created
    ON business.exa_webset_jobs (organization_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ewj_dex_run_id
    ON business.exa_webset_jobs (dex_run_id);
