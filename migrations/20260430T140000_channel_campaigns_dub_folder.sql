-- Add a Dub folder pointer to business.channel_campaigns. One Dub folder
-- per channel_campaign (the multi-step parent), used by step_link_minting
-- to organize bulk-minted links in the Dub dashboard. Sparse — only
-- direct_mail (and future link-using channels) populate it; legacy rows
-- and non-direct_mail channels never set it.

ALTER TABLE business.channel_campaigns
    ADD COLUMN IF NOT EXISTS dub_folder_id TEXT;

CREATE INDEX IF NOT EXISTS idx_channel_campaigns_dub_folder
    ON business.channel_campaigns (dub_folder_id)
    WHERE dub_folder_id IS NOT NULL;
