-- Phase 2 contract substrate — partner-authored audience specs, signings, deliveries.
--
-- The audience-spec layer is the partner-facing contract. Specs ARE
-- contracts; partners pay $25-45K per signed spec; operator carries refund
-- risk if cohort can't be maintained. This migration ships the schema only —
-- evaluator + REST surface live in app/services/audience_spec/ and
-- app/routers/audience_specs_v1.py.
--
-- Tables:
--   business.audience_specs           — partner-authored draft + revision chain
--   business.audience_spec_signings   — immutable signing event (the contract)
--   business.audience_spec_deliveries — per-cohort multi-channel events
--
-- Append-only. No vertical_id columns (per
-- vertical_network_platform_frame.md). Partner+spec is the architectural
-- primitive. References to business.organizations.id stand in for partners
-- until a dedicated business.partners table lands; orgs are the partner
-- entity in v1.

-- ────────────────────── audience_specs (drafts + revisions) ──────────────

CREATE TABLE IF NOT EXISTS business.audience_specs (
    spec_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The owning partner. References business.organizations.id; orgs are
    -- the partner entity in v1. ON DELETE RESTRICT — never cascade away
    -- a contract substrate row when the org is removed.
    partner_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,

    version INTEGER NOT NULL DEFAULT 1,

    -- Supersedes chain. NULL for v1; each new revision points at the prior
    -- spec_id so the lineage is recoverable.
    parent_spec_id UUID REFERENCES business.audience_specs(spec_id) ON DELETE SET NULL,

    -- The pydantic-validated spec body. Schema enforced at the
    -- application layer (app/services/audience_spec/models.py).
    content JSONB NOT NULL,

    -- Lifecycle. draft = under composition; preview = run against fresh
    -- catalog; signed = immutable contract minted via the signings table;
    -- superseded = newer revision exists; retired = manually closed.
    status TEXT NOT NULL CHECK (status IN ('draft','preview','signed','superseded','retired')),

    -- Per-source freshness SLA the spec declares (from
    -- AudienceSpec.required_freshness). Refused at sign-time if not met.
    -- Snapshot of [{source, max_age_seconds}, ...].
    required_freshness JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Supabase auth.users.id of whoever drafted/revised. No FK across to
    -- auth schema; we just store the uuid.
    created_by_user_id UUID,

    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_audience_specs_partner_status
    ON business.audience_specs (partner_id, status);
CREATE INDEX IF NOT EXISTS idx_audience_specs_parent
    ON business.audience_specs (parent_spec_id);

-- ────────────────────── audience_spec_signings (the contract) ────────────

CREATE TABLE IF NOT EXISTS business.audience_spec_signings (
    signing_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    spec_id UUID NOT NULL REFERENCES business.audience_specs(spec_id) ON DELETE RESTRICT,

    signed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The data-as-of timestamp the cohort was frozen against. Iceberg
    -- snapshot semantics: every row in the manifest reflects the catalog
    -- as it appeared at this instant. Refund/replenishment math anchors
    -- to this baseline.
    catalog_snapshot_ts TIMESTAMPTZ NOT NULL,

    -- Cohort size at signing — the number the partner agreed to. Refund
    -- exposure is realized when the live cohort drifts below this.
    count_at_signing INTEGER NOT NULL,

    -- R2 path to the frozen entity-ref parquet. The cohort manifest is
    -- the contract artifact: an immutable parquet file of (entity_ref,
    -- attribute_snapshot) for every entity in the cohort at signing.
    cohort_manifest_uri TEXT NOT NULL,

    -- Whatever signing artifact applies (operator note, e-sig event,
    -- payment receipt, partner-platform proposal id, etc.). Free shape;
    -- partner-platform is responsible for populating this with whatever
    -- it accepts as evidence-of-intent.
    partner_signature JSONB,

    contract_term_days INTEGER NOT NULL DEFAULT 90,
    -- ``timestamptz + interval`` is STABLE (timezone-dependent), so it
    -- can't be a generated expression. Compute at insert time instead.
    -- A trigger keeps expires_at in sync if contract_term_days is
    -- updated; updates are not expected (signings are append-only) but
    -- the trigger is cheap insurance.
    expires_at TIMESTAMPTZ NOT NULL,

    -- Snapshot of per-source freshness at the moment of signing. Lets us
    -- prove later "the freshness SLA WAS met when we signed."
    -- Shape: [{source, max_age_seconds, observed_age_seconds, ok}, ...].
    source_freshness_at_signing JSONB,

    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_signings_spec
    ON business.audience_spec_signings (spec_id);
CREATE INDEX IF NOT EXISTS idx_signings_active
    ON business.audience_spec_signings (expires_at);

-- Trigger to compute expires_at = signed_at + contract_term_days. The
-- evaluator could compute this client-side, but a trigger lets external
-- writers (operator psql, future MAGS agents, etc.) get it for free.
CREATE OR REPLACE FUNCTION business._audience_spec_signings_set_expires_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.expires_at := NEW.signed_at + make_interval(days => NEW.contract_term_days);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_audience_spec_signings_set_expires_at
    ON business.audience_spec_signings;
CREATE TRIGGER trg_audience_spec_signings_set_expires_at
    BEFORE INSERT OR UPDATE OF signed_at, contract_term_days
    ON business.audience_spec_signings
    FOR EACH ROW
    EXECUTE FUNCTION business._audience_spec_signings_set_expires_at();

-- ────────────────────── audience_spec_deliveries (cohort events) ─────────

CREATE TABLE IF NOT EXISTS business.audience_spec_deliveries (
    delivery_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signing_id UUID NOT NULL REFERENCES business.audience_spec_signings(signing_id) ON DELETE RESTRICT,

    -- Canonical entity identifier (UEI, EIN, DOT, LEI, etc.). Free text
    -- because the substrate spans multiple identity systems. The
    -- entity-resolution layer downstream maps these to canonical IDs.
    entity_ref TEXT NOT NULL,

    -- The cohort lifecycle event. entered_cohort = entity now matches
    -- the spec; surfaced = shown to partner (any channel); viewed =
    -- partner clicked through; reserved = partner held it; claimed =
    -- partner converted; dismissed = partner declined; exited_cohort =
    -- entity no longer matches; attribute_changed = material change
    -- (per operator_data_anxieties_phase_0.md wrong-match concern).
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'entered_cohort','surfaced','viewed','reserved','claimed',
        'dismissed','exited_cohort','attribute_changed'
    )),

    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Multi-channel per matches_first_class_surfacing_multichannel.md.
    -- Cold-email is a peer surfacing channel to portal; operator_intro
    -- is the high-touch path.
    channel TEXT CHECK (channel IN ('portal','cold_email','operator_intro')),

    -- What was true about the entity at this event. Captures the
    -- match-time attribute snapshot per the wrong-match-from-stale-data
    -- concern (operator_data_anxieties_phase_0.md, point 3).
    attribute_snapshot JSONB,

    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_deliveries_signing_kind
    ON business.audience_spec_deliveries (signing_id, event_kind);
CREATE INDEX IF NOT EXISTS idx_deliveries_entity
    ON business.audience_spec_deliveries (entity_ref);
