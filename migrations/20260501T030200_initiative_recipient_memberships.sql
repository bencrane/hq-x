-- GTM-initiative attribution slice 1: per-initiative recipient manifest.
--
-- Manifest of "what was paid for" per initiative. One active row per
-- (initiative, recipient) pair. Populated by the audience materializer
-- at initiative materialization time (separate directive). Read by
-- voice-agent inbound routing, billing reconciliation, and overlap-
-- detection logic.
--
-- NOT a replacement for channel_campaign_step_recipients (which is the
-- per-step membership) — this is the higher-grain "paid context" layer.

CREATE TABLE business.initiative_recipient_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    initiative_id UUID NOT NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT,
    partner_contract_id UUID NOT NULL
        REFERENCES business.partner_contracts(id) ON DELETE RESTRICT,
    recipient_id UUID NOT NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,
    -- Frozen at materialization time. Denormalized from the initiative's
    -- audience spec so historical rows survive even if the initiative's
    -- spec pointer were ever rewritten.
    data_engine_audience_id UUID NOT NULL,
    -- Optional: which step / channel_campaign was the recipient first
    -- materialized into. Useful for debug; not load-bearing.
    first_seen_channel_campaign_id UUID
        REFERENCES business.channel_campaigns(id) ON DELETE SET NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Soft-delete: when a recipient is excluded from continued outreach
    -- under this initiative (eg. unsubscribe, suppression-list match,
    -- explicit operator removal). Voice-agent routing and active-contract
    -- queries filter `WHERE removed_at IS NULL`.
    removed_at TIMESTAMPTZ,
    removed_reason TEXT
);

-- Active membership uniqueness: a recipient can be in at most one ACTIVE
-- initiative-recipient row at a time. Removed rows do not block a new
-- active row, so re-adding after removal works.
CREATE UNIQUE INDEX uq_irm_active_recipient_per_initiative
    ON business.initiative_recipient_memberships (initiative_id, recipient_id)
    WHERE removed_at IS NULL;

-- Lookup-by-recipient (voice-agent inbound resolution path):
-- "find this recipient's active paid contract."
CREATE INDEX idx_irm_recipient_active
    ON business.initiative_recipient_memberships (recipient_id, partner_contract_id)
    WHERE removed_at IS NULL;

-- Lookup-by-contract (billing reconciliation, contract-fulfillment reporting):
-- "list all recipients paid-for under this contract."
CREATE INDEX idx_irm_contract_active
    ON business.initiative_recipient_memberships (partner_contract_id, added_at)
    WHERE removed_at IS NULL;

-- Lookup-by-audience-spec (anti-double-billing across initiatives that share
-- a spec id; flag conflicts at materialization time).
CREATE INDEX idx_irm_audience_spec
    ON business.initiative_recipient_memberships (data_engine_audience_id)
    WHERE removed_at IS NULL;

COMMENT ON TABLE business.initiative_recipient_memberships IS
    'Manifest of "what was paid for" per initiative. One row per '
    '(initiative, recipient) pair. Populated by the audience materializer '
    'at initiative materialization time. Read by voice-agent inbound '
    'routing, billing reconciliation, and overlap-detection logic. '
    'NOT a replacement for channel_campaign_step_recipients (which is the '
    'per-step membership) — this is the higher-grain "paid context" layer.';
