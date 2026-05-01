-- GTM-pipeline materializer slice — extend gtm_subagent_runs with the
-- per-recipient/per-step fanout dimensions.
--
-- The foundation slice ran one row per (initiative, agent_slug, run_index).
-- The materializer slice introduces fanout: gtm-per-recipient-creative
-- (and its verdict) executes once per (recipient × DM step), so the
-- uniqueness target must include those columns.
--
-- Sentinel-uuid trick: the unique index COALESCEs NULL columns to the
-- all-zero UUID so initiative-scoped rows (recipient_id IS NULL,
-- channel_campaign_step_id IS NULL) still benefit from uniqueness while
-- per-recipient rows partition by their (recipient, step) pair.
-- 00000000-0000-0000-0000-000000000000 is not a real recipient id —
-- safe sentinel.

ALTER TABLE business.gtm_subagent_runs
    ADD COLUMN recipient_id UUID NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,
    ADD COLUMN channel_campaign_step_id UUID NULL
        REFERENCES business.channel_campaign_steps(id) ON DELETE RESTRICT;

ALTER TABLE business.gtm_subagent_runs
    DROP CONSTRAINT gtm_subagent_runs_initiative_id_agent_slug_run_index_key;

CREATE UNIQUE INDEX uq_gtm_subagent_runs_per_fanout
    ON business.gtm_subagent_runs (
        initiative_id,
        agent_slug,
        COALESCE(recipient_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(channel_campaign_step_id, '00000000-0000-0000-0000-000000000000'::uuid),
        run_index
    );

-- Lookup: every per-recipient run for an initiative (powers the
-- aggregate endpoint + admin drilldown).
CREATE INDEX idx_gtm_subagent_runs_recipient
    ON business.gtm_subagent_runs (
        initiative_id, agent_slug, recipient_id, channel_campaign_step_id
    )
    WHERE recipient_id IS NOT NULL;

COMMENT ON COLUMN business.gtm_subagent_runs.recipient_id IS
    'Set for per-recipient fanout agents (gtm-per-recipient-creative + verdict). '
    'NULL for initiative-scoped agents.';

COMMENT ON COLUMN business.gtm_subagent_runs.channel_campaign_step_id IS
    'Set when the run is scoped to a specific step (per-recipient creative). '
    'NULL for initiative-scoped agents (sequence-definer, master-strategist, '
    'channel-step-materializer, audience-materializer).';
