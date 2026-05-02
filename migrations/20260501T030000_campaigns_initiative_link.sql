-- GTM-initiative attribution slice 1: link campaigns to gtm_initiatives.
--
-- Multiple campaigns may share an initiative_id (1:many initiative →
-- campaigns). Legacy DMaaS rows predating the owned-brand pivot have
-- initiative_id IS NULL; emit_event payloads carry `initiative_id: null`
-- for them and downstream consumers handle that.
--
-- Invariant (application-enforced): when set, campaigns.brand_id MUST
-- equal gtm_initiatives.brand_id. The materializer is the only writer
-- in the owned-brand pipeline; no DB trigger enforces this. Add the
-- trigger if a violation surfaces in practice.

ALTER TABLE business.campaigns
    ADD COLUMN initiative_id UUID NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT;

CREATE INDEX idx_campaigns_initiative
    ON business.campaigns (initiative_id)
    WHERE initiative_id IS NOT NULL;

COMMENT ON COLUMN business.campaigns.initiative_id IS
    'When set, this campaign belongs to a GTM initiative (owned-brand lead-gen). '
    'Multiple campaigns may share an initiative_id (1:many). Legacy DMaaS rows '
    'predating the owned-brand pivot have initiative_id IS NULL. '
    'Invariant (application-enforced): when set, campaigns.brand_id MUST equal '
    'gtm_initiatives.brand_id. The materializer is the only writer; no DB '
    'trigger enforces this.';
