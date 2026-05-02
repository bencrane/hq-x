-- Slice 2 — DMaaS orchestration: customer-facing webhook subscriptions
-- and per-event delivery log.
--
-- Stripe / GitHub-style webhooks. Customers subscribe to event names
-- (with `*` wildcard), we POST HMAC-signed payloads to their URL, and
-- retry-with-backoff on failure. Distinct surface from RudderStack
-- (which serves customer-managed destinations like their warehouse).

CREATE TABLE business.customer_webhook_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID REFERENCES business.brands(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    -- HMAC signing key. We sign every outbound delivery with this value
    -- (hex sha256 over the raw body) and the customer verifies. The
    -- plaintext must persist because we recompute on every delivery; the
    -- secret is *also* surfaced once to the customer at create / rotate.
    -- Operators should not see this column in dashboards (treat as
    -- credential material).
    secret TEXT NOT NULL,
    -- Constant-time-comparable hash of `secret` for any future
    -- "verify the secret you stored" flow (e.g. authenticated rotate).
    secret_hash TEXT NOT NULL,
    event_filter TEXT[] NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN (
        'active', 'paused', 'delivery_failing'
    )),
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_delivery_at TIMESTAMPTZ,
    last_failure_at TIMESTAMPTZ,
    last_failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE business.customer_webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL
        REFERENCES business.customer_webhook_subscriptions(id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    event_payload JSONB NOT NULL,
    attempt INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'succeeded', 'failed', 'dead_lettered'
    )),
    response_status INT,
    response_body TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_retry_at TIMESTAMPTZ
);

CREATE INDEX idx_cws_org ON business.customer_webhook_subscriptions (organization_id);
CREATE INDEX idx_cws_state ON business.customer_webhook_subscriptions (state);
CREATE INDEX idx_cwd_subscription
    ON business.customer_webhook_deliveries (subscription_id, attempted_at DESC);
CREATE INDEX idx_cwd_pending
    ON business.customer_webhook_deliveries (next_retry_at)
    WHERE status = 'pending';
