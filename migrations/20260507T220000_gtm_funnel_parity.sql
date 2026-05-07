-- GTM funnel parity — Cluster 1 + Cluster 2 reliability surfaces.
--
-- Three additive tables, all `IF NOT EXISTS`:
--
--   1. business.cluster1_auto_replies — per-auto-reply audit row.
--      Operator runs Cluster 1 (self_prospecting) outreach to demand-side
--      partners. When a prospect replies positively, the inbox auto-reply
--      agent composes a "happy to hop on a call" reply and sends it
--      in-thread via EmailBison. This table records every such reply
--      attempt — composed, sent, deferred (operator disabled), failed.
--
--   2. business.cluster_outbound_heartbeat_log — per-cluster outbound
--      heartbeat results (cluster_1 + cluster_2). Mirrors the Cluster 3
--      heartbeat table but generalized to a `cluster` discriminator.
--
--   3. business.cluster_step_recovery_log — per-recovery-sweep result row.
--      The recovery sweep finds activation_jobs / channel_campaign_steps
--      stuck in non-terminal states past threshold, and either retries
--      or fails them with reason. One log row per sweep.
--
-- Plus two ALTERs:
--
--   * gtm_initiatives.metadata gains an implicit `cluster1_auto_reply_enabled`
--     key (no schema change — JSONB is already there). Default behavior
--     is "enabled" when key is absent. Operator sets to false to disable
--     auto-reply per-initiative.
--   * channel_campaign_step_recipients.metadata gains an implicit
--     `eb_lead_id` key (also no schema change) — populated when lead
--     attach is wired in a follow-up PR. The reliability layer surfaces
--     when the key is absent past threshold (signals lead-attach failure).

-- 1. cluster1_auto_replies
CREATE TABLE IF NOT EXISTS business.cluster1_auto_replies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    organization_id UUID NOT NULL REFERENCES business.organizations(id),
    brand_id UUID REFERENCES business.brands(id),
    initiative_id UUID NOT NULL REFERENCES business.gtm_initiatives(id),

    -- The inbound positive reply that fired this auto-reply.
    inbound_email_message_id UUID NOT NULL
        REFERENCES business.email_messages(id) ON DELETE CASCADE,
    email_reply_classification_id UUID NOT NULL
        REFERENCES business.email_reply_classifications(id) ON DELETE CASCADE,

    -- The outbound auto-reply (in-thread).
    outbound_email_message_id UUID
        REFERENCES business.email_messages(id) ON DELETE SET NULL,

    -- The EB reply id we POSTed against (the inbound reply).
    eb_inbound_reply_id BIGINT,
    -- The EB reply id of our outbound (returned by /api/replies/{id}/reply).
    eb_outbound_reply_id BIGINT,

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued',
            'sent',
            'failed',
            'deferred_disabled',
            'pending_review',
            'cancelled'
        )),

    rendered_subject TEXT,
    rendered_body_text TEXT,

    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    failure_reason TEXT,

    composer_backend TEXT,    -- 'anthropic' | 'stub'
    composer_model TEXT,
    verdict_score INTEGER,
    verdict_blockers JSONB,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One non-cancelled auto-reply per inbound classification (gates webhook
-- replays from double-firing).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_c1ar_classification_active
    ON business.cluster1_auto_replies (email_reply_classification_id)
    WHERE email_reply_classification_id IS NOT NULL
      AND status IN ('queued', 'sent', 'pending_review');

CREATE INDEX IF NOT EXISTS idx_c1ar_initiative
    ON business.cluster1_auto_replies (initiative_id);
CREATE INDEX IF NOT EXISTS idx_c1ar_status_queued_at
    ON business.cluster1_auto_replies (status, queued_at DESC);


-- 2. cluster_outbound_heartbeat_log
CREATE TABLE IF NOT EXISTS business.cluster_outbound_heartbeat_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster TEXT NOT NULL CHECK (cluster IN ('cluster_1', 'cluster_2')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'pass', 'fail')),
    duration_ms INTEGER,
    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_cohb_cluster_started
    ON business.cluster_outbound_heartbeat_log (cluster, started_at DESC);


-- 3. cluster_step_recovery_log
CREATE TABLE IF NOT EXISTS business.cluster_step_recovery_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'pass', 'fail')),
    duration_ms INTEGER,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    retried INTEGER NOT NULL DEFAULT 0,
    abandoned INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_csr_started
    ON business.cluster_step_recovery_log (started_at DESC);
