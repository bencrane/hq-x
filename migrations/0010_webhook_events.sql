-- Migration 0010: webhook_events + cal_raw_events — backfill / extend
--
-- These tables exist in the dev DB ahead of any hq-x migration (legacy from
-- OEX-era schema). This migration:
--   - creates them IF NOT EXISTS for fresh environments
--   - adds the brand_id column (single-operator world) for both tables
--   - adds an index for brand-scoped lookups on webhook_events
--
-- Existing org_id / company_id columns on webhook_events are left alone;
-- new code paths write brand_id and ignore the legacy columns.

CREATE TABLE IF NOT EXISTS webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_slug TEXT NOT NULL,
    event_key TEXT NOT NULL,
    event_type TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    replay_count INT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE webhook_events
    ADD COLUMN IF NOT EXISTS brand_id UUID REFERENCES business.brands(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event_key
    ON webhook_events(provider_slug, event_key);
CREATE INDEX IF NOT EXISTS idx_webhook_events_brand
    ON webhook_events(brand_id) WHERE brand_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cal_raw_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_event TEXT,
    payload JSONB NOT NULL,
    cal_event_uid TEXT,
    organizer_email TEXT,
    attendee_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    event_type_id BIGINT,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cal_raw_events_uid
    ON cal_raw_events(cal_event_uid) WHERE cal_event_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cal_raw_events_processed
    ON cal_raw_events(processed) WHERE processed = FALSE;
