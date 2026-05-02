-- Denormalize the Dub folder + tags onto the dmaas_dub_links join row at
-- insert time, so analytics joins can filter without round-tripping Dub.
-- Both default empty/null for legacy rows; new bulk-mint paths populate
-- them from the Dub link payload.

ALTER TABLE dmaas_dub_links
    ADD COLUMN IF NOT EXISTS dub_folder_id TEXT,
    ADD COLUMN IF NOT EXISTS dub_tag_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_folder
    ON dmaas_dub_links (dub_folder_id) WHERE dub_folder_id IS NOT NULL;
