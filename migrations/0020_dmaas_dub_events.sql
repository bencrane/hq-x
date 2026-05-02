-- Append-only projection of Dub webhook events (link.clicked, lead.created,
-- sale.created). Raw envelopes still land in `webhook_events` for audit /
-- replay; this table is the queryable surface for attribution joins.
--
-- One row per (dub_event_id). dub_link_id is denormalized so attribution
-- queries don't need to round-trip through dmaas_dub_links.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS dmaas_dub_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Dub's event id (`evt_…` or webhook `id`). Unique → safe replay target.
    dub_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('link.clicked', 'lead.created', 'sale.created')),
    -- 'link_…' from Dub. Not a FK because we may receive an event for a
    -- link we never minted ourselves (manual creation in Dub UI, or a link
    -- created by another integration). Soft join via dmaas_dub_links.
    dub_link_id TEXT,
    -- When the event happened upstream (Dub's `createdAt`). Distinct from
    -- created_at, which is when we received it.
    occurred_at TIMESTAMPTZ NOT NULL,
    -- Optional structured fields. Each event type populates a subset; the
    -- full envelope is always available in `payload` for whatever else.
    click_country TEXT,
    click_city TEXT,
    click_device TEXT,
    click_browser TEXT,
    click_os TEXT,
    click_referer TEXT,
    customer_id TEXT,
    customer_email TEXT,
    sale_amount_cents BIGINT,
    sale_currency TEXT,
    -- Full upstream payload for the event (Dub's `data` block).
    payload JSONB NOT NULL,
    -- Link back to the webhook_events row that produced this projection.
    webhook_event_id UUID REFERENCES webhook_events(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dmaas_dub_events_link
    ON dmaas_dub_events (dub_link_id) WHERE dub_link_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dmaas_dub_events_type_occurred
    ON dmaas_dub_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_dmaas_dub_events_customer
    ON dmaas_dub_events (customer_id) WHERE customer_id IS NOT NULL;
