-- Partner Platform bootstrap.
--
-- Adds the formal link from platform tenants (business.organizations) to CRM
-- accounts (business.accounts), seeds the partner-platform quarterly product,
-- and bootstraps Revenue Activation as the first partner account.
--
-- The auth.users row + business.users row + organization_membership for
-- benjamin.crane@revenueactivation.io are created out-of-band via the Supabase
-- admin API after this migration runs (separate seed script — auth.users is
-- not safe to write via SQL).

-- 1. Tenant ↔ CRM account link.
ALTER TABLE business.organizations
    ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES business.accounts(id);

CREATE INDEX IF NOT EXISTS idx_organizations_account
    ON business.organizations (account_id) WHERE account_id IS NOT NULL;

-- 2. Catalog: partner platform quarterly access SKU.
INSERT INTO business.products (
    sku, name, description, billing_model, unit_price_cents, billing_period, metadata, active
)
VALUES (
    'partner-platform-quarterly',
    'Partner Platform — Quarterly Access',
    'Quarterly access to the partner platform: audience composition, lead transfer pricing, market signals.',
    'subscription',
    0,
    'quarter',
    '{}'::jsonb,
    true
)
ON CONFLICT (sku) DO NOTHING;

-- 3. Revenue Activation as CRM account.
INSERT INTO business.accounts (name, domain, status, metadata)
VALUES (
    'Revenue Activation',
    'revenueactivation.io',
    'active',
    '{"seed":"partner-platform-bootstrap"}'::jsonb
)
ON CONFLICT DO NOTHING;

-- 4. Benjamin Crane as primary contact.
INSERT INTO business.contacts (
    account_id, first_name, last_name, email, role, is_primary
)
SELECT id, 'Benjamin', 'Crane', 'benjamin.crane@revenueactivation.io', 'buyer', true
FROM business.accounts WHERE domain = 'revenueactivation.io'
ON CONFLICT DO NOTHING;

-- 5. Platform tenant for Revenue Activation, linked to CRM account.
INSERT INTO business.organizations (name, slug, status, plan, account_id, metadata)
SELECT
    'Revenue Activation',
    'revenue-activation',
    'active',
    'partner_quarterly',
    id,
    '{}'::jsonb
FROM business.accounts WHERE domain = 'revenueactivation.io'
ON CONFLICT (slug) DO UPDATE SET account_id = EXCLUDED.account_id;

-- 6. Active quarterly purchase, period_end 90 days from now — the access gate.
INSERT INTO business.account_purchases (
    account_id, product_id, quantity, unit_price_cents, total_cents, currency,
    status, purchased_at, period_start, period_end, contact_id, metadata
)
SELECT
    a.id, p.id, 1, 0, 0, 'USD',
    'active', NOW(), NOW(), NOW() + INTERVAL '90 days',
    c.id, '{"seed":"partner-platform-bootstrap"}'::jsonb
FROM business.accounts a
JOIN business.products p ON p.sku = 'partner-platform-quarterly'
LEFT JOIN business.contacts c ON c.account_id = a.id AND c.is_primary
WHERE a.domain = 'revenueactivation.io';
