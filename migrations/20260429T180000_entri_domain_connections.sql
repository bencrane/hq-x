-- Entri custom-domain integration.
--
-- Each row represents one customer-owned hostname (e.g. "qr.acme.com" or
-- "acme.com") that we serve through Entri's reverse proxy on behalf of a
-- direct-mail campaign step. Lifecycle:
--
--   pending_modal          : we minted a JWT, frontend is opening the modal
--   dns_records_submitted  : Entri's onSuccess fired (records written but
--                            possibly not yet propagated)
--   live                   : webhook confirmed propagation + Power + Secure
--   failed                 : 72h propagation timeout or persistent error
--   disconnected           : customer revoked / we DELETE'd from Entri Power
--
-- Webhooks land in the existing `webhook_events` table with
-- provider_slug='entri' and event_key=payload.id. This table is the
-- *projection* — the read-side state used by the rest of the app.

CREATE TABLE IF NOT EXISTS business.entri_domain_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    -- Optional: which step this domain serves. Null = org-level (e.g.
    -- branded short-link domain not bound to a single mailer).
    channel_campaign_step_id UUID
        REFERENCES business.channel_campaign_steps(id) ON DELETE SET NULL,

    domain TEXT NOT NULL,                       -- "qr.acme.com" (full FQDN)
    is_root_domain BOOLEAN NOT NULL DEFAULT FALSE,

    -- Where Entri's reverse proxy forwards traffic to. Carried in the
    -- dnsRecords config + registered via PUT /power.
    application_url TEXT NOT NULL,

    state TEXT NOT NULL DEFAULT 'pending_modal'
        CHECK (state IN (
            'pending_modal',
            'dns_records_submitted',
            'live',
            'failed',
            'disconnected'
        )),

    -- Stringified "<organization_id>:<step_id>" — round-trips through
    -- Entri as the `userId` config field and back as `user_id` on every
    -- webhook payload. Sole correlation key for inbound webhooks.
    entri_user_id TEXT NOT NULL,

    -- Latest minted JWT for this session (60-min TTL on Entri's side).
    -- Stored so retries within the modal flow reuse rather than mint anew.
    entri_token TEXT,
    entri_token_expires_at TIMESTAMPTZ,

    -- Reflected from webhook payloads. Free-form; no CHECK because Entri
    -- can add new providers / statuses without notice.
    provider TEXT,                              -- "godaddy" | "cloudflare" | ...
    setup_type TEXT,                            -- "automatic" | "manual" | "sharedLogin"
    propagation_status TEXT,
    power_status TEXT,
    secure_status TEXT,

    last_webhook_id TEXT,                       -- payload.id of most recent event
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Two campaigns can't claim the same hostname at the same time. Disconnected
-- and failed rows are excluded so the same domain can be re-onboarded.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entri_domain_active
    ON business.entri_domain_connections(domain)
    WHERE state IN ('pending_modal', 'dns_records_submitted', 'live');

CREATE INDEX IF NOT EXISTS idx_entri_domain_org
    ON business.entri_domain_connections(organization_id);

CREATE INDEX IF NOT EXISTS idx_entri_domain_step
    ON business.entri_domain_connections(channel_campaign_step_id)
    WHERE channel_campaign_step_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entri_domain_user_id
    ON business.entri_domain_connections(entri_user_id);

CREATE INDEX IF NOT EXISTS idx_entri_domain_state
    ON business.entri_domain_connections(state);

-- Standard updated_at trigger pattern used elsewhere in the schema.
CREATE OR REPLACE FUNCTION business.touch_entri_domain_connections()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entri_domain_touch ON business.entri_domain_connections;
CREATE TRIGGER trg_entri_domain_touch
    BEFORE UPDATE ON business.entri_domain_connections
    FOR EACH ROW EXECUTE FUNCTION business.touch_entri_domain_connections();
