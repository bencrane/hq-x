-- Tenancy migration: introduce organizations as the root tenant for paying-customer
-- operational data. business.brands becomes a child of an organization. User-to-brand
-- 1:1 (business.users.client_id) is replaced by organization_memberships.
--
-- Two role axes are introduced:
--   * platform_role on business.users (global; currently only 'platform_operator')
--   * org_role on business.organization_memberships (per-membership)
--
-- business.accounts / business.contacts / business.products / business.account_purchases
-- are NOT touched: they are the outbound CRM hierarchy and are unrelated to platform
-- tenancy.
--
-- Backfill rules (run in this migration):
--   1. Create one operator workspace organization.
--   2. Assign all existing brands to it.
--   3. Make all existing users members of it (operators → org_role='owner' +
--      platform_role='platform_operator'; clients → org_role='member').
--   4. Backfill brand_id on direct_mail_pieces, business.audience_drafts,
--      cal_raw_events by joining created_by_user_id → business.users.client_id.
--      Orphans are left NULL pending manual review (NOT NULL is NOT applied to
--      these tables in this migration).
--
-- After backfill:
--   * brands.organization_id is set NOT NULL.
--   * dmaas_designs.brand_id is set NOT NULL (any legacy NULLs must be resolved
--     out-of-band before this migration runs in prd; the migration aborts via
--     constraint violation if they exist, which is the desired behavior).

-- ── 1. New tables ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business.organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'trial', 'suspended', 'churned', 'archived')),
    plan TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS business.organization_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES business.users(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE CASCADE,
    org_role TEXT NOT NULL CHECK (org_role IN ('owner', 'admin', 'member')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'invited', 'suspended', 'removed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, organization_id)
);

CREATE INDEX IF NOT EXISTS idx_org_memberships_org
    ON business.organization_memberships (organization_id);
CREATE INDEX IF NOT EXISTS idx_org_memberships_user
    ON business.organization_memberships (user_id);

CREATE TABLE IF NOT EXISTS business.audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    actor_platform_role TEXT,
    organization_id UUID REFERENCES business.organizations(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_org_created
    ON business.audit_events (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor_created
    ON business.audit_events (actor_user_id, created_at DESC);

-- ── 2. Column additions (nullable; backfilled below) ──────────────────────

ALTER TABLE business.users
    ADD COLUMN IF NOT EXISTS platform_role TEXT
        CHECK (platform_role IS NULL OR platform_role IN ('platform_operator'));

ALTER TABLE business.brands
    ADD COLUMN IF NOT EXISTS organization_id UUID
        REFERENCES business.organizations(id);

ALTER TABLE direct_mail_pieces
    ADD COLUMN IF NOT EXISTS brand_id UUID
        REFERENCES business.brands(id) ON DELETE SET NULL;

ALTER TABLE business.audience_drafts
    ADD COLUMN IF NOT EXISTS brand_id UUID
        REFERENCES business.brands(id) ON DELETE SET NULL;

ALTER TABLE cal_raw_events
    ADD COLUMN IF NOT EXISTS brand_id UUID
        REFERENCES business.brands(id) ON DELETE SET NULL;

-- ── 3. Backfill ───────────────────────────────────────────────────────────

-- Operator workspace organization. Created idempotently keyed on slug.
INSERT INTO business.organizations (name, slug, status)
VALUES ('Operator Workspace', 'operator-workspace', 'active')
ON CONFLICT (slug) DO NOTHING;

-- Assign every existing brand to the operator workspace.
UPDATE business.brands
SET organization_id = (
    SELECT id FROM business.organizations WHERE slug = 'operator-workspace'
)
WHERE organization_id IS NULL;

-- Stamp platform_role on existing operator users.
UPDATE business.users
SET platform_role = 'platform_operator'
WHERE role = 'operator' AND platform_role IS NULL;

-- Memberships: every existing user joins the operator workspace.
INSERT INTO business.organization_memberships (user_id, organization_id, org_role, status)
SELECT
    u.id,
    (SELECT id FROM business.organizations WHERE slug = 'operator-workspace'),
    CASE WHEN u.role = 'operator' THEN 'owner' ELSE 'member' END,
    'active'
FROM business.users u
ON CONFLICT (user_id, organization_id) DO NOTHING;

-- Backfill brand_id on operationally-scoped tables that were missing it.
-- Joins created_by_user_id → business.users.client_id (the legacy 1:1 link).
-- Where the user has no client_id, brand_id stays NULL (orphan, manual review).

UPDATE direct_mail_pieces dmp
SET brand_id = u.client_id
FROM business.users u
WHERE dmp.created_by_user_id = u.id
  AND dmp.brand_id IS NULL
  AND u.client_id IS NOT NULL;

UPDATE business.audience_drafts ad
SET brand_id = u.client_id
FROM business.users u
WHERE ad.created_by_user_id = u.id
  AND ad.brand_id IS NULL
  AND u.client_id IS NOT NULL;

-- cal_raw_events has no created_by_user_id in current schema; leave NULL for
-- manual review. The column is in place for future scoping.

-- ── 4. Tighten constraints post-backfill ──────────────────────────────────

ALTER TABLE business.brands
    ALTER COLUMN organization_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_brands_organization_id
    ON business.brands (organization_id);

-- dmaas_designs.brand_id cleanup: tighten only if no legacy NULLs remain.
-- (If NULLs exist, this fails and the operator must clean them up first.)
ALTER TABLE dmaas_designs
    ALTER COLUMN brand_id SET NOT NULL;

-- direct_mail_pieces.brand_id, business.audience_drafts.brand_id,
-- cal_raw_events.brand_id remain NULLABLE pending operator review of orphans.
