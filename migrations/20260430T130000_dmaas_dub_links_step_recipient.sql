-- Add channel_campaign_step_id + recipient_id to dmaas_dub_links so step
-- launch can mint one Dub link per recipient and re-launch is idempotent
-- (partial unique index below).

ALTER TABLE dmaas_dub_links
    ADD COLUMN IF NOT EXISTS channel_campaign_step_id UUID
        REFERENCES business.channel_campaign_steps(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS recipient_id UUID
        REFERENCES business.recipients(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_step
    ON dmaas_dub_links (channel_campaign_step_id)
    WHERE channel_campaign_step_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_recipient
    ON dmaas_dub_links (recipient_id)
    WHERE recipient_id IS NOT NULL;

-- A recipient gets at most one Dub link per step. Re-running launch after
-- a transient failure must re-use the existing row, not double-mint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dmaas_dub_links_step_recipient
    ON dmaas_dub_links (channel_campaign_step_id, recipient_id)
    WHERE channel_campaign_step_id IS NOT NULL AND recipient_id IS NOT NULL;
