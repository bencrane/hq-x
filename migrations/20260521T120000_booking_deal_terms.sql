-- Booking deal terms: the engagement-proposal terms an operator fills out
-- after a Cal.com call, keyed to the booking.
--
-- One row per booking (cal_event_uid). The terms live in a JSONB payload so
-- the post-call form's field set can evolve without a migration. Current
-- shape:
--   { company_name, domain, price_cents, duration_days,
--     success_fee_tiers: [{ bps, up_to_cents }] }
-- success_fee_tiers is an ordered list of marginal bps bands over aggregate
-- disbursed capital; the final tier carries up_to_cents = null ("and above").
--
-- This payload is what later renders into the prospect-facing proposal.

CREATE TABLE IF NOT EXISTS business.deal_terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The Cal.com booking this proposal is for. Not a FK: cal_raw_events
    -- holds many rows per cal_event_uid (one per lifecycle event), so the
    -- uid is not unique there. Unique here — one deal-terms record per
    -- booking; the post-call form upserts on it.
    cal_event_uid TEXT NOT NULL UNIQUE,

    terms JSONB NOT NULL,

    created_by_user_id UUID
        REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- cal_event_uid already has a UNIQUE constraint (= index) for the upsert.
