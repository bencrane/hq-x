-- EmailBison: per-recipient artifact table for the email channel.
--
-- Mirrors the shape of direct_mail_pieces (denormalized hierarchy +
-- recipient_id + provider external ids + status). All columns nullable
-- except the FKs and timestamps so legacy ad-hoc sends can land later.
--
-- (eb_workspace_id, eb_scheduled_email_id) is THE per-message webhook
-- reconciliation key per docs/emailbison-api-mcp-coverage.md §5.

CREATE TABLE IF NOT EXISTS business.email_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- denormalized hierarchy (matches direct_mail_pieces convention)
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL
        REFERENCES business.brands(id) ON DELETE RESTRICT,
    campaign_id UUID
        REFERENCES business.campaigns(id) ON DELETE SET NULL,
    channel_campaign_id UUID
        REFERENCES business.channel_campaigns(id) ON DELETE SET NULL,
    channel_campaign_step_id UUID
        REFERENCES business.channel_campaign_steps(id) ON DELETE SET NULL,
    recipient_id UUID
        REFERENCES business.recipients(id) ON DELETE SET NULL,

    -- EmailBison external identity
    eb_workspace_id          TEXT,
    eb_lead_id               BIGINT,
    eb_campaign_id           BIGINT,
    eb_scheduled_email_id    BIGINT,
    eb_sequence_step_id      BIGINT,
    eb_sender_email_id       BIGINT,
    raw_message_id           TEXT,

    -- send-time snapshot (for legal / spam-complaint replay)
    subject_snapshot         TEXT,
    body_snapshot            TEXT,
    sender_email_snapshot    TEXT,

    -- aggregated counters projected from webhook events
    sent_at                  TIMESTAMPTZ,
    last_opened_at           TIMESTAMPTZ,
    open_count               INT NOT NULL DEFAULT 0,
    bounced_at               TIMESTAMPTZ,
    replied_at               TIMESTAMPTZ,
    unsubscribed_at          TIMESTAMPTZ,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'scheduled', 'sent', 'opened', 'replied',
            'bounced', 'unsubscribed', 'failed'
        )),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Webhook reconciliation key
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_messages_eb_scheduled
    ON business.email_messages (eb_workspace_id, eb_scheduled_email_id)
    WHERE eb_scheduled_email_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_email_messages_step
    ON business.email_messages (channel_campaign_step_id)
    WHERE channel_campaign_step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_email_messages_recipient
    ON business.email_messages (recipient_id)
    WHERE recipient_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_email_messages_eb_campaign
    ON business.email_messages (eb_campaign_id)
    WHERE eb_campaign_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_email_messages_status
    ON business.email_messages (status, organization_id);


CREATE TABLE IF NOT EXISTS business.email_message_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_message_id UUID NOT NULL
        REFERENCES business.email_messages(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    raw_event_name TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_message_events_msg
    ON business.email_message_events (email_message_id, occurred_at);

-- Idempotency guard for re-projections of the same webhook event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_message_events_dedup
    ON business.email_message_events (
        email_message_id, raw_event_name, occurred_at
    );
