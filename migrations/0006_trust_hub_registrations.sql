-- Migration 0006: trust_hub_registrations — Twilio Trust Hub state machine
--
-- Ports OEX migration 036, swapping (org_id, company_id) for brand_id.
-- One registration per brand per type — single-operator world removes the
-- per-company multiplication.

CREATE TABLE IF NOT EXISTS trust_hub_registrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,

    registration_type TEXT NOT NULL CHECK (registration_type IN (
        'customer_profile', 'shaken_stir', 'a2p_campaign', 'cnam'
    )),

    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'pending-review', 'in-review',
        'twilio-approved', 'twilio-rejected', 'failed'
    )),

    bundle_sid TEXT,
    policy_sid TEXT,

    -- Component SIDs (populated during customer_profile creation).
    end_user_business_sid TEXT,
    end_user_rep1_sid TEXT,
    end_user_rep2_sid TEXT,
    address_sid TEXT,
    supporting_document_sid TEXT,

    -- For dependent registrations (a2p_campaign etc.): the CustomerProfile
    -- bundle SID this depends on.
    customer_profile_sid TEXT,

    evaluation_sid TEXT,
    evaluation_status TEXT
        CHECK (evaluation_status IS NULL OR evaluation_status IN ('compliant', 'noncompliant')),
    evaluation_results JSONB,

    error_details JSONB,
    notification_email TEXT,

    submitted_at TIMESTAMPTZ,
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One registration per brand per type.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trust_hub_reg_brand_type
    ON trust_hub_registrations(brand_id, registration_type);

CREATE INDEX IF NOT EXISTS idx_trust_hub_reg_brand
    ON trust_hub_registrations(brand_id);
CREATE INDEX IF NOT EXISTS idx_trust_hub_reg_status
    ON trust_hub_registrations(brand_id, status);
CREATE INDEX IF NOT EXISTS idx_trust_hub_reg_bundle_sid
    ON trust_hub_registrations(bundle_sid)
    WHERE bundle_sid IS NOT NULL;
