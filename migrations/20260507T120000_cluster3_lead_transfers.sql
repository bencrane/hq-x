-- Cluster 3 v1 — lead_transfers ledger + per-cluster source-material artifacts.
--
-- Cluster 3 fires when a supply-side recipient replies positively to a
-- Cluster 2 outreach. The orchestrator looks up which demand-side partner
-- is attached to the audience reservation, allocates the transfer against
-- the partner_contract's paid count, composes a merchant-banker-tone
-- intro (separate new-thread message — not in-thread reply), and sends
-- via EmailBison.
--
-- Three additive tables, all guarded by IF NOT EXISTS:
--
--   1. business.lead_transfers
--      One row per intro that Cluster 3 attempts to ship. Status walks
--      queued → sent (or failed / deferred_capped / cancelled). This is
--      the canonical answer to "how many transfers has partner X
--      received against contract Y in audience Z?" — used by the
--      allocation gate before composing the next intro.
--
--   2. business.partner_research_artifacts
--      Per-partner Exa-fed research synthesis, persisted so the Cluster
--      3 intro composer can cite the demand-side partner's
--      differentiators with concrete language. UPSERT on partner_id —
--      re-running the synthesis agent overwrites in place.
--
--   3. business.audience_context_artifacts
--      Per-audience aggregated context (e.g. "this audience is motor
--      carriers that lost insurance in the past 30 days — common pain
--      X, Y, Z"). Reusable across every partner who reserves that
--      audience. UPSERT on data_engine_audience_id.
--
-- Both artifact tables are read by the Cluster 3 intro composer; both
-- gracefully render "(no artifact yet)" when missing so the bootstrap
-- window doesn't hard-break.

-- 1. lead_transfers ledger.
CREATE TABLE IF NOT EXISTS business.lead_transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    organization_id UUID NOT NULL REFERENCES business.organizations(id),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    partner_id UUID NOT NULL REFERENCES business.demand_side_partners(id),
    partner_contract_id UUID NOT NULL REFERENCES business.partner_contracts(id),

    -- Cross-DB pointer; no FK (audience_specs lives in DEX).
    data_engine_audience_id UUID NOT NULL,

    leg2_initiative_id UUID NOT NULL REFERENCES business.gtm_initiatives(id),
    leg3_initiative_id UUID REFERENCES business.gtm_initiatives(id),

    -- The supply-side person being introduced.
    recipient_id UUID REFERENCES business.recipients(id) ON DELETE SET NULL,

    -- The inbound positive reply that fired this transfer.
    positive_reply_email_message_id UUID
        REFERENCES business.email_messages(id) ON DELETE SET NULL,
    email_reply_classification_id UUID
        REFERENCES business.email_reply_classifications(id) ON DELETE SET NULL,

    -- The outbound intro email sent to the supply-side person.
    intro_email_message_id UUID
        REFERENCES business.email_messages(id) ON DELETE SET NULL,

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'sent', 'failed', 'deferred_capped', 'cancelled'
        )),

    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    failure_reason TEXT,

    -- Allocation snapshot at decision time (paid count, delivered count
    -- at time of this transfer, contract window remaining, etc.) for
    -- forensic replay if the allocation rule changes later.
    allocation_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lt_partner_audience_status
    ON business.lead_transfers (partner_id, data_engine_audience_id, status);

CREATE INDEX IF NOT EXISTS idx_lt_contract
    ON business.lead_transfers (partner_contract_id);

CREATE INDEX IF NOT EXISTS idx_lt_leg2_initiative
    ON business.lead_transfers (leg2_initiative_id);

-- Concurrency guard: only one non-cancelled transfer per classification
-- so concurrent webhook replays can't double-spend allocation.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_lt_classification_active
    ON business.lead_transfers (email_reply_classification_id)
    WHERE email_reply_classification_id IS NOT NULL
      AND status IN ('queued', 'sent');


-- 2. partner_research_artifacts — Cluster 2 source material per partner.
CREATE TABLE IF NOT EXISTS business.partner_research_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL UNIQUE
        REFERENCES business.demand_side_partners(id) ON DELETE CASCADE,

    research_md TEXT NOT NULL,

    source_records JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_by TEXT NOT NULL DEFAULT 'partner-research-synthesis-agent',
    model TEXT,
    duration_ms INTEGER,
    cost_dollars NUMERIC(10, 6),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- 3. audience_context_artifacts — Cluster 2 source material per audience.
CREATE TABLE IF NOT EXISTS business.audience_context_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Cross-DB pointer (DEX ops.audience_specs.id).
    data_engine_audience_id UUID NOT NULL UNIQUE,
    audience_template_slug TEXT,

    context_md TEXT NOT NULL,

    source_records JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_by TEXT NOT NULL DEFAULT 'audience-context-synthesis-agent',
    model TEXT,
    duration_ms INTEGER,
    cost_dollars NUMERIC(10, 6),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aca_template_slug
    ON business.audience_context_artifacts (audience_template_slug)
    WHERE audience_template_slug IS NOT NULL;
