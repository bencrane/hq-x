-- Reserved-audience tie-in: an organization claims a frozen
-- data-engine-x ops.audience_specs row as "their" audience for downstream
-- DM creative work. data_engine_audience_id IS the spec id (no second
-- identifier minted in DEX). Cross-DB so it's not a real FK; the hq-x
-- HTTP layer verifies the spec exists in DEX at reservation time.
--
-- Distinct from business.audience_drafts (user-owned, pre-reservation,
-- no DEX spec yet). This table is org-owned, post-reservation, points to
-- a real DEX spec id.

CREATE TABLE IF NOT EXISTS business.org_audience_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE CASCADE,
    -- The DEX ops.audience_specs.id. Not a FK (cross-DB). hq-x trusts the
    -- caller to pass a real id; client-layer verifies on creation.
    data_engine_audience_id UUID NOT NULL,
    -- Cached at reservation time so the row is self-describing without a
    -- DEX round-trip. Source of truth still lives in DEX.
    source_template_slug TEXT NOT NULL,
    source_template_id UUID NOT NULL,
    audience_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'active', 'paused', 'cancelled')),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reserved_by_user_id UUID
        REFERENCES business.users(id) ON DELETE SET NULL,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, data_engine_audience_id)
);

CREATE INDEX IF NOT EXISTS idx_org_audience_reservations_org
    ON business.org_audience_reservations (organization_id);
CREATE INDEX IF NOT EXISTS idx_org_audience_reservations_audience
    ON business.org_audience_reservations (data_engine_audience_id);
