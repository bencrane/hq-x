-- DMaaS scaffolds: add `strategy` column.
--
-- Strategy is the communication intent of a scaffold (hero / proof / offer
-- / trust). Used by the user-facing chat agent to pick a layout that
-- matches the campaign brief without inspecting DSL geometry. Nullable for
-- back-compat with scaffolds authored before the column existed.
--
-- This migration uses a UTC-timestamp filename (YYYYMMDDTHHMMSS_<slug>.sql)
-- per the convention introduced alongside the face-consistency change.
-- The migration runner (scripts/migrate.py) applies files in lexical
-- order; timestamps lexically sort after legacy numeric prefixes.

ALTER TABLE dmaas_scaffolds
    ADD COLUMN IF NOT EXISTS strategy TEXT
        CHECK (strategy IN ('hero', 'proof', 'offer', 'trust'));

CREATE INDEX IF NOT EXISTS idx_dmaas_scaffolds_strategy
    ON dmaas_scaffolds(strategy)
    WHERE strategy IS NOT NULL;

UPDATE dmaas_scaffolds SET strategy = 'hero'
WHERE slug = 'hero-headline-postcard' AND strategy IS NULL;
