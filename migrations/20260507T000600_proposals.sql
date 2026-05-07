-- Proposals: prospect-facing agreements that become demand-side
-- partnerships once paid.
--
-- Lifecycle:
--   1. Operator creates a proposal in admin (proposes audience, count,
--      price/transfer, window). Service mints (or reuses) a
--      demand_side_partners row + a partner_contracts row in 'draft'
--      and links them here. Mints a unique public_token.
--   2. Operator sends prospect the URL `/proposal/<public_token>`.
--   3. Prospect lands on partner-platform, sees proposal, optionally
--      modifies the audience (resolves a different DEX audience_specs id
--      via the audience composer), then proceeds to checkout.
--   4. Stripe checkout session is minted; payment can be CC or ACH.
--   5. On `checkout.session.completed`: status flips to 'paid',
--      partner_contracts.status flips to 'active', and
--      ca_svc.instantiate_for_payment fires Cluster 2.
--
-- The table holds both the negotiation surface (proposed_*) and the
-- final state (final_*) so we have a clean audit of what was offered
-- vs. what was paid for.

CREATE TABLE IF NOT EXISTS business.proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL
        REFERENCES business.brands(id) ON DELETE RESTRICT,
    partner_id UUID NOT NULL
        REFERENCES business.demand_side_partners(id) ON DELETE RESTRICT,
    partner_contract_id UUID NOT NULL
        REFERENCES business.partner_contracts(id) ON DELETE RESTRICT,

    -- DEX `ops.audience_specs.id`. Cross-DB; not a FK. Proposed at create
    -- time; final_data_engine_audience_id captures what the prospect
    -- actually confirmed (may differ if they modified via the composer).
    proposed_data_engine_audience_id UUID NOT NULL,
    final_data_engine_audience_id UUID,

    -- Proposed economics. partner_contracts row mirrors these fields at
    -- payment time; we keep them here too so the proposal is
    -- self-describing without the contract round-trip.
    proposed_transfer_count INTEGER NOT NULL
        CHECK (proposed_transfer_count > 0),
    proposed_price_per_transfer_cents BIGINT NOT NULL
        CHECK (proposed_price_per_transfer_cents > 0),
    proposed_window_days INTEGER NOT NULL
        CHECK (proposed_window_days > 0),

    -- Total = count × price/transfer. Generated to prevent drift.
    proposed_total_cents BIGINT GENERATED ALWAYS AS
        (proposed_transfer_count::BIGINT * proposed_price_per_transfer_cents)
        STORED,

    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft',
        'sent',
        'viewed',
        'audience_confirmed',
        'checkout_initiated',
        'paid',
        'expired',
        'cancelled'
    )),

    -- Public unguessable token used in /proposal/<token> URL. URL-safe.
    public_token TEXT NOT NULL UNIQUE,

    -- Prospect contact (denormalized for audit; canonical on partner row).
    prospect_company_name TEXT NOT NULL,
    prospect_contact_name TEXT,
    prospect_contact_email TEXT,

    -- Stripe linkage (populated lazily as flow progresses).
    stripe_checkout_session_id TEXT,
    stripe_payment_intent_id TEXT,
    paid_at TIMESTAMPTZ,
    paid_amount_cents BIGINT,

    -- Cluster 2 instantiation result (populated after webhook fires).
    instantiated_leg2_initiative_id UUID
        REFERENCES business.gtm_initiatives(id) ON DELETE SET NULL,
    instantiated_at TIMESTAMPTZ,

    sent_at TIMESTAMPTZ,
    viewed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id UUID
        REFERENCES business.users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_status
    ON business.proposals (status);
CREATE INDEX IF NOT EXISTS idx_proposals_partner
    ON business.proposals (partner_id);
CREATE INDEX IF NOT EXISTS idx_proposals_org_status
    ON business.proposals (organization_id, status);
CREATE INDEX IF NOT EXISTS idx_proposals_audience_proposed
    ON business.proposals (proposed_data_engine_audience_id);
-- public_token already has UNIQUE constraint on the column.
