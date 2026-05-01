-- GTM-pipeline foundation §4.1 — registry mapping local slugs to Anthropic
-- Managed Agents API agent ids.
--
-- Each subagent in the post-payment pipeline is registered once with the
-- Managed Agents API; this table holds the (slug, anthropic_agent_id, role)
-- mapping plus per-agent metadata. role='actor' produces output, role='verdict'
-- pairs to an actor and decides ship/redo. role='critic' is reserved for the
-- post-foundation sub-squad split (not used in v0). role='orchestrator'
-- reserves slot for any future top-level coordinator agent.
--
-- parent_actor_slug ties verdict/critic rows back to the actor whose output
-- they reason over. NULL for actor/orchestrator rows.
--
-- The model column lets the operator dial individual agents down to Sonnet
-- (or up to whatever ships next) without touching code. Default `claude-opus-4-7`
-- per the foundation directive's "Opus across the board until validated".
--
-- deactivated_at is a soft-delete: a slug can be retired without losing the
-- foreign-key target on historical gtm_subagent_runs rows.

CREATE TABLE IF NOT EXISTS business.gtm_agent_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_slug TEXT NOT NULL UNIQUE,
    anthropic_agent_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('actor', 'verdict', 'critic', 'orchestrator')),
    -- For role='verdict' or 'critic': the agent_slug of the paired actor.
    -- Not a FK so we can register an actor + verdict in either order without
    -- ordering pain at setup-script time. Application layer enforces the link.
    parent_actor_slug TEXT,
    model TEXT NOT NULL DEFAULT 'claude-opus-4-7',
    description TEXT,
    deactivated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS gtm_agent_registry_role_idx
    ON business.gtm_agent_registry (role)
    WHERE deactivated_at IS NULL;

CREATE INDEX IF NOT EXISTS gtm_agent_registry_parent_idx
    ON business.gtm_agent_registry (parent_actor_slug)
    WHERE deactivated_at IS NULL AND parent_actor_slug IS NOT NULL;
