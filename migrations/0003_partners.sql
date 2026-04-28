-- Migration 0003: partners — client companies served by the operator
--
-- Was `companies` in OEX. Renamed for clarity in single-operator world.
-- A partner belongs to exactly one brand. Campaigns may be partner-dedicated
-- (partner_id set) or shared across N partners in a vertical (partner_id null).

CREATE TABLE IF NOT EXISTS business.partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    name TEXT NOT NULL,

    -- Business hours by day-of-week + timezone, e.g.
    --   {"timezone": "America/New_York",
    --    "weekly": {"mon": [["09:00","17:00"]], "tue": ..., "sat": [], "sun": []}}
    availability_schedule JSONB,

    -- Date-keyed overrides:
    --   {"2026-12-25": {"closed": true}, "2026-12-31": {"hours": [["09:00","13:00"]]}}
    availability_overrides JSONB,

    -- Default transfer destination for warm transfers.
    transfer_phone TEXT,
    transfer_label TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_partners_brand
    ON business.partners(brand_id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_partners_brand_name
    ON business.partners(brand_id, name) WHERE deleted_at IS NULL;

-- Composite-FK target for campaigns / voice tables.
CREATE UNIQUE INDEX IF NOT EXISTS uq_partners_id_brand
    ON business.partners(id, brand_id);
