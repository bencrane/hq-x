-- business.exa_research_jobs — orchestration row for every async Exa
-- research run. Mirrors business.activation_jobs in shape (status enum,
-- JSONB payload/error/history, trigger_run_id, idempotency uniqueness)
-- so the operator surface, reconciliation crons, and observability
-- patterns stay consistent across DMaaS and Exa work.
--
-- result_ref is a stringy pointer to the actual raw payload, since the
-- payload may live in either DB:
--    'hqx://exa.exa_calls/<uuid>'  or  'dex://exa.exa_calls/<uuid>'

CREATE TABLE business.exa_research_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    endpoint TEXT NOT NULL CHECK (endpoint IN (
        'search', 'contents', 'find_similar', 'research', 'answer'
    )),
    destination TEXT NOT NULL CHECK (destination IN ('hqx', 'dex')),
    objective TEXT NOT NULL,
    objective_ref TEXT,
    request_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_lettered'
    )),
    result_ref TEXT,
    error JSONB,
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    trigger_run_id TEXT,
    idempotency_key TEXT,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_erj_org_idempotency
    ON business.exa_research_jobs (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_erj_status ON business.exa_research_jobs (status);
CREATE INDEX idx_erj_org_created
    ON business.exa_research_jobs (organization_id, created_at DESC);
CREATE INDEX idx_erj_objective
    ON business.exa_research_jobs (objective, objective_ref);
