-- exa.exa_calls — raw archive for every Exa API call run from hq-x.
--
-- Mirror schema lives in data-engine-x; the destination is per-run
-- (hqx | dex) so customer-scoped research lands here and dataset-grade
-- enrichment lands next to the entities it informs.
--
-- This table is a request/response audit log: never normalize, never
-- transform inside the persistence path. Per-objective derived tables
-- come in follow-up directives.

CREATE SCHEMA IF NOT EXISTS exa;

CREATE TABLE IF NOT EXISTS exa.exa_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint TEXT NOT NULL CHECK (endpoint IN (
        'search', 'contents', 'find_similar', 'research', 'answer'
    )),
    request_payload JSONB NOT NULL,
    response_payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'succeeded', 'failed'
    )),
    error TEXT,
    -- Echoed from Exa response when present.
    exa_request_id TEXT,
    cost_dollars NUMERIC(10, 6),
    duration_ms INTEGER,
    -- Lightweight, untyped backref so consumers can find their data.
    -- Convention: '<resource>:<id>' e.g. 'reservation:abc-123', 'company:dot:12345'.
    objective TEXT NOT NULL,
    objective_ref TEXT,
    -- Pointer back to the orchestrating hq-x job id for joinability across
    -- DBs. Same UUID lives in business.exa_research_jobs.id in hq-x.
    triggered_by_job_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_exa_calls_objective
    ON exa.exa_calls (objective, objective_ref);
CREATE INDEX IF NOT EXISTS idx_exa_calls_job
    ON exa.exa_calls (triggered_by_job_id);
CREATE INDEX IF NOT EXISTS idx_exa_calls_created
    ON exa.exa_calls (created_at DESC);
