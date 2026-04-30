-- Slice 1 — DMaaS orchestration: activation_jobs durable job table.
--
-- Every long-running operation (campaign activation, step activation,
-- step scheduled-activation) creates a row here and runs through a
-- Trigger.dev task that calls back into hq-x's /internal endpoints.
-- Postgres is the source of truth for job state; Trigger.dev is the
-- executor.
--
-- Idempotency-Key (organization_id, idempotency_key) is unique when set
-- so replays of the customer-facing async endpoints return the original
-- job_id rather than spawning a duplicate.

CREATE TABLE business.activation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN (
        'dmaas_campaign_activation',
        'step_activation',
        'step_scheduled_activation'
    )),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_lettered'
    )),
    idempotency_key TEXT,
    payload JSONB NOT NULL,
    result JSONB,
    error JSONB,
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    trigger_run_id TEXT,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_aj_org_idempotency
    ON business.activation_jobs (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_aj_status ON business.activation_jobs (status);
CREATE INDEX idx_aj_org_created ON business.activation_jobs (organization_id, created_at DESC);
