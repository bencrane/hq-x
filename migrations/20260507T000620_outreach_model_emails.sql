-- Outreach model emails: operator-curated reference outreach copy used as
-- few-shot voice/style anchors for per-recipient creative generation.
--
-- Selector keys for retrieval at email-gen time:
--   * organization_id (always)
--   * purpose (which cluster the model serves: demand_side_outreach,
--     supply_side_opt_in, lead_intro, general)
--   * audience_template_slug (optional; e.g. 'fmcsa-motor-carriers' to
--     anchor models to a specific audience type)
--   * step_index (optional; 1-indexed step number for sequence-anchored
--     models, e.g. step 1 = cold open, step 2 = follow-up)
--
-- The per-recipient creative input bundle pulls a small set (default
-- limit: 3) ordered by best-match-then-recency. Operator's voice survives
-- the LLM call by being demonstrated, not described.

CREATE TABLE IF NOT EXISTS business.outreach_model_emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE CASCADE,
    brand_id UUID
        REFERENCES business.brands(id) ON DELETE SET NULL,

    purpose TEXT NOT NULL CHECK (purpose IN (
        'demand_side_outreach',
        'supply_side_opt_in',
        'lead_intro',
        'general'
    )),
    audience_template_slug TEXT,
    step_index INTEGER CHECK (step_index IS NULL OR step_index > 0),

    label TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    notes TEXT,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id UUID
        REFERENCES business.users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ome_lookup
    ON business.outreach_model_emails (
        organization_id, purpose, audience_template_slug, step_index
    )
    WHERE is_active = TRUE;
