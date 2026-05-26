-- business.agent_runs — lightweight ledger for Anthropic Managed Agents
-- sessions opened against the `gtm-agent` (see scripts/managed_agents/
-- provision.py). One row per session minted via POST /api/v1/agent-runs.
--
-- session_id is Anthropic's own ID (sesn_*) and is unique. Anthropic owns
-- the event history server-side; this table holds only the metadata hq-x
-- needs to attribute the session to a caller / signal and to render an
-- admin "recent runs" view. Token usage is backfilled from Anthropic's
-- session.usage object after the session goes idle.
--
-- Schema choice: `business.*` matches hq-x's other operational tables
-- (gtm_agent_registry, agent_prompt_versions, etc.). NOT `ops.*` or
-- `entities.*` — those belong to DEX.
--
-- Forward-only migration per hq-x convention (IF NOT EXISTS everywhere).
-- Re-applies cleanly after `git revert`.

CREATE SCHEMA IF NOT EXISTS business;

CREATE TABLE IF NOT EXISTS business.agent_runs (
    session_id       TEXT PRIMARY KEY,
    agent_id         TEXT        NOT NULL,
    environment_id   TEXT        NOT NULL,
    signal_slug      TEXT        NULL,
    user_id          UUID        NOT NULL,
    initial_message  TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'starting',
    stop_reason      JSONB       NULL,
    usage            JSONB       NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recent-runs admin view: ORDER BY created_at DESC LIMIT N
CREATE INDEX IF NOT EXISTS agent_runs_created_at_desc_idx
    ON business.agent_runs (created_at DESC);

-- Per-user history (My Runs page) + per-signal history (signal detail page)
CREATE INDEX IF NOT EXISTS agent_runs_user_id_created_at_desc_idx
    ON business.agent_runs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_runs_signal_slug_created_at_desc_idx
    ON business.agent_runs (signal_slug, created_at DESC)
    WHERE signal_slug IS NOT NULL;

-- Status pivot for "in-flight" dashboards (cheap because most rows are
-- terminal — partial index keeps the BTREE small).
CREATE INDEX IF NOT EXISTS agent_runs_status_inflight_idx
    ON business.agent_runs (status)
    WHERE status IN ('starting', 'running');
