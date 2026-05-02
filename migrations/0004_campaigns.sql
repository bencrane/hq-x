-- Migration 0004: campaigns — operational unit (a "play")
--
-- Was `company_campaigns` in OEX. Renamed for clarity. A campaign belongs to
-- exactly one brand. partner_id null means shared across N partners in a
-- vertical (resolved at runtime via reservation lookup — deferred to v2).

CREATE TABLE IF NOT EXISTS business.campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NULL,

    name TEXT NOT NULL,
    vertical TEXT,

    -- Whose brand identity goes on outbound mail/email/calls.
    mailer_brand_mode TEXT NOT NULL DEFAULT 'operator'
        CHECK (mailer_brand_mode IN ('operator', 'partner')),

    -- Dedicated phone-number pool per campaign vs shared across partners.
    number_strategy TEXT NOT NULL DEFAULT 'dedicated'
        CHECK (number_strategy IN ('dedicated', 'shared')),

    -- For shared campaigns: how is the responding partner resolved at runtime.
    routing_key TEXT NOT NULL DEFAULT 'number'
        CHECK (routing_key IN ('number', 'code')),

    -- Default conversational substrate. Vapi for new campaigns unless they opt
    -- into the TwiML IVR engine (FMCSA-style menus).
    assistant_substrate TEXT NOT NULL DEFAULT 'vapi'
        CHECK (assistant_substrate IN ('vapi', 'twiml_ivr')),

    status TEXT NOT NULL DEFAULT 'active',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    -- Composite FK: a campaign's partner must belong to the same brand.
    CONSTRAINT campaigns_same_brand_fk
        FOREIGN KEY (partner_id, brand_id)
        REFERENCES business.partners(id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_campaigns_brand
    ON business.campaigns(brand_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_campaigns_partner
    ON business.campaigns(partner_id)
    WHERE partner_id IS NOT NULL AND deleted_at IS NULL;

-- Composite-FK target for voice/SMS child tables.
CREATE UNIQUE INDEX IF NOT EXISTS uq_campaigns_id_brand
    ON business.campaigns(id, brand_id);
