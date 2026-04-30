-- Landing-page render tracking.
--
-- Every server-side render of /lp/{step_id}/{short_code} that resolves
-- a recipient writes a row here AND fires emit_event("page.viewed", ...).
-- The recipient timeline (Slice 5) joins this table alongside
-- direct_mail_piece_events and dmaas_dub_events to surface the full
-- "delivered → clicked → viewed → submitted" funnel per recipient.
--
-- A separate table (rather than just relying on emit_event log fan-out)
-- because:
--   1. Six-tuple-tagged events go to RudderStack/ClickHouse — querying
--      back per-recipient timelines is slow and cluster-dependent.
--   2. The recipient timeline endpoint is already a multi-table join in
--      Postgres; adding one more table is cheaper than building a
--      ClickHouse query path for hq-x dashboards.
--
-- source_metadata holds the rate-limit-keying material (hashed IP, UA,
-- referrer). Hashed-only — raw IPs never land here.

CREATE TABLE IF NOT EXISTS business.landing_page_views (
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

    -- {ip_hash, user_agent, referrer, ...}. Raw IPs never persisted.
    source_metadata JSONB,

    viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lpv_org_brand
    ON business.landing_page_views(organization_id, brand_id);

CREATE INDEX IF NOT EXISTS idx_lpv_step
    ON business.landing_page_views(channel_campaign_step_id);

CREATE INDEX IF NOT EXISTS idx_lpv_recipient
    ON business.landing_page_views(recipient_id);

CREATE INDEX IF NOT EXISTS idx_lpv_viewed_at
    ON business.landing_page_views(viewed_at DESC);

-- Speeds up the in-app rate-limit dedupe: "did this ip_hash render this
-- step in the last 60s?" The expression index isn't strictly required
-- because the time index above is enough, but keeps the per-step lookup
-- cheap when one step is hammered.
CREATE INDEX IF NOT EXISTS idx_lpv_step_recent
    ON business.landing_page_views(channel_campaign_step_id, viewed_at DESC);
