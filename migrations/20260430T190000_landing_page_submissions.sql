-- Form submissions captured from hosted landing pages.
--
-- One row per `POST /lp/{step_id}/{short_code}/submit` that passes
-- form_schema validation + the honeypot check. The form_data column is
-- raw JSONB keyed by the field names from the step's
-- landing_page_config.cta.form_schema — every campaign can have its
-- own shape.
--
-- This is the customer's lead pipeline. Once leads land here, switching
-- DMaaS providers means extracting the full table — that's a deliberate
-- product moat.
--
-- source_metadata holds {ip_hash, user_agent, referrer, geo?}. Raw IPs
-- never persisted; the caller hashes before insert.

CREATE TABLE IF NOT EXISTS business.landing_page_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL
        REFERENCES business.brands(id) ON DELETE RESTRICT,
    campaign_id UUID NOT NULL
        REFERENCES business.campaigns(id) ON DELETE RESTRICT,
    channel_campaign_id UUID NOT NULL
        REFERENCES business.channel_campaigns(id) ON DELETE RESTRICT,
    channel_campaign_step_id UUID NOT NULL
        REFERENCES business.channel_campaign_steps(id) ON DELETE RESTRICT,
    recipient_id UUID NOT NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,

    form_data JSONB NOT NULL,
    source_metadata JSONB,

    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lps_org_brand
    ON business.landing_page_submissions(organization_id, brand_id);

CREATE INDEX IF NOT EXISTS idx_lps_step
    ON business.landing_page_submissions(channel_campaign_step_id);

CREATE INDEX IF NOT EXISTS idx_lps_recipient
    ON business.landing_page_submissions(recipient_id);

CREATE INDEX IF NOT EXISTS idx_lps_submitted_at
    ON business.landing_page_submissions(submitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_lps_channel_campaign
    ON business.landing_page_submissions(channel_campaign_id);
