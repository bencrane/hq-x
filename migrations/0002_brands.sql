-- Migration 0002: brands — compliance/marketing identity (1..N)
--
-- Each brand owns its own Twilio account_sid + auth_token (encrypted at rest
-- via pgcrypto pgp_sym_encrypt with BRAND_CREDS_ENCRYPTION_KEY). Multiple
-- brands may live under one legal entity. Brand owns Trust Hub registration,
-- A2P 10DLC bundle, caller-ID display, and a Twilio number pool.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS business.brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    display_name TEXT,
    domain TEXT,

    -- Twilio credentials, encrypted via pgp_sym_encrypt at write,
    -- decrypted via pgp_sym_decrypt at use. Plaintext never lands in the row.
    twilio_account_sid_enc BYTEA,
    twilio_auth_token_enc BYTEA,

    -- Not secret: just an opaque SID. Kept plaintext for index/lookup.
    twilio_messaging_service_sid TEXT,

    -- Primary CustomerProfile bundle SID (set after first Trust Hub registration).
    primary_customer_profile_sid TEXT,

    -- The currently-active CustomerProfile registration row in trust_hub_registrations.
    -- Nullable until first registration completes.
    trust_hub_registration_id UUID NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_brands_name
    ON business.brands(name) WHERE deleted_at IS NULL;

-- Composite-FK target: child tables enforce (*, brand_id) → brands(id, _) at the DB layer.
CREATE UNIQUE INDEX IF NOT EXISTS uq_brands_id_brand
    ON business.brands(id);
