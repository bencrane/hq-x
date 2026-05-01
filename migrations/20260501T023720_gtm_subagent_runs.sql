-- GTM-pipeline foundation §4.3 — run capture for every actor + verdict
-- invocation in the post-payment pipeline.
--
-- This table is the spine of the debugging story. The frontend reads from
-- here to render the per-initiative pipeline timeline; rerun-step writes a
-- new row with run_index = max + 1 and marks downstream rows superseded;
-- prompt-version-id links each run to the exact system prompt active at run
-- start so iterating prompts is auditable.
--
-- Replay semantics:
--   * Rerunning a step inserts a new row with run_index = max + 1.
--   * The previous row stays at its terminal status (e.g. 'succeeded').
--   * Downstream rows for the same initiative are bulk-marked 'superseded'
--     before the new run starts.
--
-- output_artifact_path is for steps whose output is a large markdown
-- document (e.g. master strategist) — the JSONB column gets a pointer
-- instead of the full body. Leave NULL for inline outputs.

CREATE TABLE IF NOT EXISTS business.gtm_subagent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    initiative_id UUID NOT NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE CASCADE,
    agent_slug TEXT NOT NULL,
    run_index INT NOT NULL,
    -- Verdict rows link to their actor's run via parent_run_id so the
    -- verdict→actor pairing is queryable without re-joining on agent_slug.
    parent_run_id UUID REFERENCES business.gtm_subagent_runs(id),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'superseded'
    )),
    input_blob JSONB NOT NULL,
    output_blob JSONB,
    output_artifact_path TEXT,
    -- Verbatim copy of the system prompt active when the run started.
    -- Captured even though prompt_version_id can resolve it, because the
    -- version row may be deleted in a clean-up sweep and the run history
    -- should remain self-describing.
    system_prompt_snapshot TEXT NOT NULL,
    prompt_version_id UUID REFERENCES business.agent_prompt_versions(id),
    anthropic_agent_id TEXT NOT NULL,
    anthropic_session_id TEXT,
    -- Array of Anthropic request ids returned across the session events.
    -- Useful for cross-referencing with Anthropic's logs.
    anthropic_request_ids JSONB,
    -- Structured trace of MCP tool calls inside the session. Shape is the
    -- consumer's choice; the v0 service writes a list of
    -- {tool_name, args_summary, result_summary, ms} dicts.
    mcp_calls JSONB,
    cost_cents INT,
    model TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_blob JSONB,
    UNIQUE (initiative_id, agent_slug, run_index)
);

CREATE INDEX IF NOT EXISTS gtm_subagent_runs_initiative_idx
    ON business.gtm_subagent_runs (initiative_id, started_at DESC);
CREATE INDEX IF NOT EXISTS gtm_subagent_runs_status_idx
    ON business.gtm_subagent_runs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS gtm_subagent_runs_slug_idx
    ON business.gtm_subagent_runs (agent_slug, started_at DESC);
