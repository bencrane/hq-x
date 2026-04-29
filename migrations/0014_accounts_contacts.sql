-- Migration 0013: accounts + contacts + products + purchases
--
-- accounts  = paying companies (customers / prospects we bill)
-- contacts  = people at those accounts (buyer, signer, billing, etc.)
-- products  = catalog of things accounts can pay for
-- account_purchases = what a given account actually paid for (one row per
--                     line item / subscription / one-time charge)
--
-- Intentionally lightweight — prototype scaffolding, not final billing schema.

CREATE TABLE IF NOT EXISTS business.accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name TEXT NOT NULL,
    domain TEXT,
    industry TEXT,
    employee_count INT,
    annual_revenue_usd BIGINT,

    -- Free-form for prototyping (HQ address, billing address, notes, tags...).
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Lifecycle: lead | trial | active | churned | archived
    status TEXT NOT NULL DEFAULT 'lead',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_domain
    ON business.accounts(domain) WHERE deleted_at IS NULL AND domain IS NOT NULL;


CREATE TABLE IF NOT EXISTS business.contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES business.accounts(id) ON DELETE CASCADE,

    first_name TEXT,
    last_name TEXT,
    title TEXT,
    email TEXT,
    phone TEXT,

    -- Role at the account: buyer | signer | billing | technical | other
    role TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_contacts_account
    ON business.contacts(account_id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_account_email
    ON business.contacts(account_id, lower(email))
    WHERE deleted_at IS NULL AND email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_account_primary
    ON business.contacts(account_id) WHERE is_primary AND deleted_at IS NULL;


CREATE TABLE IF NOT EXISTS business.products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,

    -- one_time | subscription | usage
    billing_model TEXT NOT NULL,
    unit_price_cents INT NOT NULL,
    -- For subscriptions: month | year. NULL for one_time / usage.
    billing_period TEXT,

    -- e.g. {"included_minutes": 1000, "channels": ["sms","voice"]}
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS business.account_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES business.accounts(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES business.products(id),

    -- Snapshot of price at purchase time (products may change later).
    quantity INT NOT NULL DEFAULT 1,
    unit_price_cents INT NOT NULL,
    total_cents INT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',

    -- pending | active | cancelled | refunded | expired
    status TEXT NOT NULL DEFAULT 'active',

    purchased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- For subscriptions; NULL for one-time.
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,

    -- Who signed off (optional).
    contact_id UUID REFERENCES business.contacts(id),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_purchases_account
    ON business.account_purchases(account_id);
CREATE INDEX IF NOT EXISTS idx_purchases_product
    ON business.account_purchases(product_id);
CREATE INDEX IF NOT EXISTS idx_purchases_status_period
    ON business.account_purchases(status, period_end);


-- ───────────────────────────── dummy seed data ─────────────────────────────
-- Idempotent: keyed off domain so re-running the migration is safe.

DO $$
DECLARE
    v_acme   UUID;
    v_globex UUID;
BEGIN
    INSERT INTO business.accounts (name, domain, industry, employee_count, annual_revenue_usd, status)
    VALUES ('Acme Logistics', 'acme.test', 'trucking', 120, 25000000, 'active')
    ON CONFLICT DO NOTHING;

    INSERT INTO business.accounts (name, domain, industry, employee_count, annual_revenue_usd, status)
    VALUES ('Globex Freight', 'globex.test', 'trucking', 40, 6000000, 'trial')
    ON CONFLICT DO NOTHING;

    SELECT id INTO v_acme   FROM business.accounts WHERE domain = 'acme.test';
    SELECT id INTO v_globex FROM business.accounts WHERE domain = 'globex.test';

    INSERT INTO business.contacts (account_id, first_name, last_name, title, email, phone, role, is_primary)
    VALUES
        (v_acme, 'Alice', 'Mendez', 'VP Operations', 'alice@acme.test', '+15555550101', 'buyer',   TRUE),
        (v_acme, 'Bob',   'Tanaka', 'Controller',    'bob@acme.test',   '+15555550102', 'billing', FALSE)
    ON CONFLICT DO NOTHING;

    INSERT INTO business.contacts (account_id, first_name, last_name, title, email, phone, role, is_primary)
    VALUES
        (v_globex, 'Carla', 'Singh', 'Founder', 'carla@globex.test', '+15555550201', 'buyer', TRUE)
    ON CONFLICT DO NOTHING;
END $$;
