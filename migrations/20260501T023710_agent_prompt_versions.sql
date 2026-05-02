-- GTM-pipeline foundation §4.2 — versioned history of agent system prompts.
--
-- Anthropic's POST /v1/agents/{id} replaces the system prompt destructively.
-- To preserve rollback targets we'd otherwise lose, the activate endpoint
-- snapshots the current Anthropic state INTO this table BEFORE pushing the
-- new prompt. Each activate produces TWO rows:
--   * activation_source='snapshot' — the soon-to-be-overwritten state
--   * activation_source='frontend_activate' (or 'rollback') — the new prompt
--
-- version_index is monotonic per agent_slug. UNIQUE (agent_slug, version_index)
-- guarantees no duplicates if two operators race; the higher index wins.
--
-- parent_version_id links a rollback row back to the version it cloned, so
-- the version-history UI can render branching when the operator rolls back
-- and then forward again.

CREATE TABLE IF NOT EXISTS business.agent_prompt_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_slug TEXT NOT NULL,
    -- Anthropic's agent id at the time this version was activated. Captured
    -- per-row so a re-registration (which mints a new anthropic_agent_id) is
    -- traceable in the history.
    anthropic_agent_id TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    version_index INT NOT NULL,
    activation_source TEXT NOT NULL CHECK (activation_source IN (
        'setup_script',         -- written when the setup script first registers the agent
        'frontend_activate',    -- new prompt pushed via the admin UI
        'rollback',             -- new prompt sourced from a prior version
        'snapshot'              -- captured-from-Anthropic state before an activate
    )),
    parent_version_id UUID REFERENCES business.agent_prompt_versions(id),
    activated_by_user_id UUID,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_slug, version_index)
);

CREATE INDEX IF NOT EXISTS agent_prompt_versions_slug_idx
    ON business.agent_prompt_versions (agent_slug, version_index DESC);
