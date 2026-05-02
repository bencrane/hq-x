-- GTM-initiative pipeline §8 data model — slice 1.
--
-- A "GTM initiative" couples a Ben-owned brand, a paying demand-side
-- partner, that partner's contract, and a frozen DEX audience spec into
-- the campaign-strategy artifact that downstream materializers
-- (channels, recipients, per-recipient creative) will consume.
--
-- This slice only models the pre-materialization states. Downstream
-- statuses (`materializing`, `ready_to_launch`, `active`, `completed`,
-- `cancelled`) are present in the enum so future directives don't have
-- to migrate the check constraint.

CREATE TABLE IF NOT EXISTS business.demand_side_partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    domain TEXT,
    primary_contact_name TEXT,
    primary_contact_email TEXT,
    primary_phone TEXT,
    intro_email TEXT,
    hours_of_operation_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_dsp_org_name
    ON business.demand_side_partners (organization_id, name)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_dsp_org
    ON business.demand_side_partners (organization_id)
    WHERE deleted_at IS NULL;


CREATE TABLE IF NOT EXISTS business.partner_contracts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL
        REFERENCES business.demand_side_partners(id) ON DELETE RESTRICT,
    pricing_model TEXT NOT NULL CHECK (pricing_model IN (
        'flat_90d', 'per_lead', 'residual_pct', 'hybrid'
    )),
    amount_cents BIGINT,
    duration_days INTEGER NOT NULL DEFAULT 90,
    max_capital_outlay_cents BIGINT,
    qualification_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    terms_blob TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'draft', 'active', 'fulfilled', 'cancelled'
    )),
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pc_partner_status
    ON business.partner_contracts (partner_id, status);


CREATE TABLE IF NOT EXISTS business.gtm_initiatives (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL
        REFERENCES business.brands(id) ON DELETE RESTRICT,
    partner_id UUID NOT NULL
        REFERENCES business.demand_side_partners(id) ON DELETE RESTRICT,
    partner_contract_id UUID NOT NULL
        REFERENCES business.partner_contracts(id) ON DELETE RESTRICT,
    -- The DEX `ops.audience_specs.id`. Not a FK (cross-DB). Locked at
    -- initiative creation. Independent of business.org_audience_reservations
    -- for the prototype; reconciliation between the two is a future
    -- directive.
    data_engine_audience_id UUID NOT NULL,
    -- Pointer to the partner-research exa.exa_calls row. Format mirrors
    -- exa_research_jobs.result_ref: '<destination>://exa.exa_calls/<uuid>'.
    partner_research_ref TEXT,
    -- Pointer to the strategic-context-research exa.exa_calls row,
    -- populated by subagent 1's completion callback.
    strategic_context_research_ref TEXT,
    -- Path on disk to the campaign_strategy.md artifact, populated by
    -- subagent 2.
    campaign_strategy_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft',
        'awaiting_strategic_research',
        'strategic_research_ready',
        'awaiting_strategy_synthesis',
        'strategy_ready',
        'failed',
        'materializing',
        'ready_to_launch',
        'active',
        'completed',
        'cancelled'
    )),
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    reservation_window_start TIMESTAMPTZ,
    reservation_window_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_gtm_org_status
    ON business.gtm_initiatives (organization_id, status);
CREATE INDEX idx_gtm_partner
    ON business.gtm_initiatives (partner_id);
CREATE INDEX idx_gtm_brand
    ON business.gtm_initiatives (brand_id);
CREATE INDEX idx_gtm_audience
    ON business.gtm_initiatives (data_engine_audience_id);
