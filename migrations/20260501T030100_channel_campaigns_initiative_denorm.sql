-- GTM-initiative attribution slice 1: denormalize initiative_id onto
-- channel_campaigns for fast emit_event resolution.
--
-- This avoids one join per analytics event. Application-maintained:
-- whatever code sets campaigns.initiative_id MUST also set the same
-- value on every child channel_campaigns row. The materializer is the
-- only such writer in the owned-brand pipeline.

ALTER TABLE business.channel_campaigns
    ADD COLUMN initiative_id UUID NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT;

CREATE INDEX idx_channel_campaigns_initiative
    ON business.channel_campaigns (initiative_id)
    WHERE initiative_id IS NOT NULL;

COMMENT ON COLUMN business.channel_campaigns.initiative_id IS
    'Denormalized from parent campaigns.initiative_id for fast emit_event '
    'resolution (avoids one join per analytics event). Application-maintained: '
    'whatever code sets campaigns.initiative_id MUST also set the same value '
    'on every child channel_campaigns row. The materializer is the only such '
    'writer in the owned-brand pipeline.';
