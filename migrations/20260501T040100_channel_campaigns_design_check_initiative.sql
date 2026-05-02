-- GTM-pipeline materializer slice — relax the direct_mail design_id
-- requirement when the channel_campaign rolls up under a GTM initiative.
--
-- The original constraint
--   (channel != 'direct_mail' OR design_id IS NOT NULL OR status = 'archived')
-- was written for the DMaaS path, where every direct_mail channel_campaign
-- shares one creative across the whole audience and design_id is required
-- before sending. The owned-brand pipeline is per-recipient: every piece
-- carries unique creative produced downstream, so the channel-level
-- design_id is not the source of truth and is left NULL at materialization
-- time. We additionally permit initiative_id IS NOT NULL as a third
-- escape hatch.
--
-- DMaaS rows (initiative_id IS NULL) still require design_id — no change
-- to the existing send path.

ALTER TABLE business.channel_campaigns
    DROP CONSTRAINT channel_campaigns_design_required_for_direct_mail;

ALTER TABLE business.channel_campaigns
    ADD CONSTRAINT channel_campaigns_design_required_for_direct_mail
    CHECK (
        channel != 'direct_mail'
        OR design_id IS NOT NULL
        OR status = 'archived'
        OR initiative_id IS NOT NULL
    );

COMMENT ON CONSTRAINT channel_campaigns_design_required_for_direct_mail
    ON business.channel_campaigns IS
    'DMaaS rows (initiative_id IS NULL) require design_id for direct_mail. '
    'Owned-brand pipeline rows (initiative_id IS NOT NULL) carry per-recipient '
    'creative downstream, so channel-level design_id is not load-bearing and '
    'may be NULL at materialization time.';
