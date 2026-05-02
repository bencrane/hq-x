-- Add `voice_inbound` to the allowed channel set on channel_campaigns.
--
-- The GTM-pipeline materializer (post-payment subagent #3) emits
-- `voice_inbound` for the AI-agent inbound surface that recipients
-- call in to. The existing CHECK predates the owned-brand pivot and
-- only listed direct_mail / email / voice_outbound / sms. Adding the
-- new value keeps the constraint protective while permitting the
-- post-payment pipeline.

ALTER TABLE business.channel_campaigns
    DROP CONSTRAINT IF EXISTS campaigns_channel_check;

ALTER TABLE business.channel_campaigns
    ADD CONSTRAINT campaigns_channel_check
    CHECK (channel = ANY (ARRAY[
        'direct_mail'::text,
        'email'::text,
        'voice_outbound'::text,
        'voice_inbound'::text,
        'sms'::text
    ]));
