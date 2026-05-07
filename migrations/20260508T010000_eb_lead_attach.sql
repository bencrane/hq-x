-- EB lead-attach — closes the outbound send loop for Cluster 1 + Cluster 2.
--
-- Until now, EmailBisonAdapter.activate_step created the EB campaign
-- object but never attached our recipients as leads, so EB had nothing
-- to actually send. Operator's production reality has been manual
-- EB-UI lead attachment per campaign.
--
-- This migration adds:
--
--   1. business.eb_lead_attach_log — one row per attempt (per step,
--      per cluster). Records the outcome: dry_run | live_pass | live_fail.
--      Operator can audit every lead-attach attempt and trace which
--      memberships landed in EB vs which failed.
--
--   2. channel_campaign_step_recipients.eb_lead_id (BIGINT) — the EB
--      lead id we got back from bulk_upsert_leads. Indexed for fast
--      "which memberships made it to EB" queries.
--
--   3. channel_campaign_step_recipients.eb_lead_attached_at TIMESTAMPTZ
--      — when the attach happened. Populated by the lead-attach
--      service.
--
--   4. channel_campaign_step_recipients.eb_lead_attach_failure_reason TEXT
--      — captured on per-recipient failure (e.g. invalid email, EB
--      rate-limit, etc.).
--
-- All additive. No data migration needed.

CREATE TABLE IF NOT EXISTS business.eb_lead_attach_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,

    channel_campaign_step_id UUID NOT NULL
        REFERENCES business.channel_campaign_steps(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES business.organizations(id),
    initiative_id UUID NOT NULL REFERENCES business.gtm_initiatives(id),
    cluster TEXT NOT NULL CHECK (cluster IN ('cluster_1', 'cluster_2')),

    -- Outcome of the attach attempt.
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN (
            'running', 'dry_run', 'live_pass', 'live_fail', 'skipped'
        )),

    -- Operating mode resolution at the time of this attempt.
    mode TEXT NOT NULL DEFAULT 'dry_run'
        CHECK (mode IN ('dry_run', 'live')),
    mode_reason TEXT,  -- why dry_run / why live (initiative flag, org flag, kill switch)

    -- Counts.
    recipients_total INTEGER NOT NULL DEFAULT 0,
    recipients_eligible INTEGER NOT NULL DEFAULT 0,
    upserted_count INTEGER NOT NULL DEFAULT 0,
    attached_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,

    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eblal_step_started
    ON business.eb_lead_attach_log (channel_campaign_step_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_eblal_initiative_started
    ON business.eb_lead_attach_log (initiative_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_eblal_cluster_started
    ON business.eb_lead_attach_log (cluster, started_at DESC);


ALTER TABLE business.channel_campaign_step_recipients
    ADD COLUMN IF NOT EXISTS eb_lead_id BIGINT;
ALTER TABLE business.channel_campaign_step_recipients
    ADD COLUMN IF NOT EXISTS eb_lead_attached_at TIMESTAMPTZ;
ALTER TABLE business.channel_campaign_step_recipients
    ADD COLUMN IF NOT EXISTS eb_lead_attach_failure_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_ccsr_eb_lead
    ON business.channel_campaign_step_recipients (eb_lead_id)
    WHERE eb_lead_id IS NOT NULL;

-- Fast "which memberships still need lead-attach" query — used by the
-- recovery sweep to find scheduled rows without eb_lead_id past
-- threshold.
CREATE INDEX IF NOT EXISTS idx_ccsr_scheduled_no_lead
    ON business.channel_campaign_step_recipients (channel_campaign_step_id, processed_at)
    WHERE status = 'scheduled' AND eb_lead_id IS NULL;
