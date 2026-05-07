-- Raw Stripe webhook event archive.
--
-- Webhook handler at /webhooks/stripe inserts every received event here
-- (idempotent on stripe_event_id) BEFORE business processing. This gives
-- us a replayable audit log and decouples receive-and-ack from
-- business-side side effects. Processing stamps processed_at on success
-- or processing_error on failure.
--
-- proposal_id is set when an event resolves to a proposals row (via
-- payment_intent or checkout_session id). Most events for our flow will
-- resolve; a few (e.g. account.updated) won't and stay NULL.

CREATE TABLE IF NOT EXISTS business.stripe_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stripe's `evt_*` id; idempotency anchor on retry.
    stripe_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    livemode BOOLEAN NOT NULL,
    api_version TEXT,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    processing_error TEXT,
    proposal_id UUID
        REFERENCES business.proposals(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_type_received
    ON business.stripe_events (event_type, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_stripe_events_unprocessed
    ON business.stripe_events (received_at)
    WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_stripe_events_proposal
    ON business.stripe_events (proposal_id)
    WHERE proposal_id IS NOT NULL;
