-- business.brand_content — DB mirror of data/brands/<slug>/*.md (+ brand.json).
--
-- Source of truth for brand voice / positioning / audience-pain / etc. is the
-- on-disk .md tree under `data/brands/<slug>/`. This table is a queryable
-- copy so a managed agent without filesystem access can read brand context
-- by (brand_id, content_key). Sync via `scripts/sync_brand_content.py`.
--
-- §9.4 of docs/strategic-direction-owned-brand-leadgen.md keeps the
-- long-term storage decision open. This table is the "for moment" middle
-- ground: disk + DB, kept in sync by a one-shot script.

CREATE TABLE IF NOT EXISTS business.brand_content (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    -- Filename without extension. e.g. 'positioning', 'voice',
    -- 'audience-pain', 'capital-types', 'creative-directives',
    -- 'industries', 'proof-and-credibility', 'value-props',
    -- 'README', 'brand' (for brand.json).
    content_key TEXT NOT NULL,
    content_format TEXT NOT NULL DEFAULT 'md'
        CHECK (content_format IN ('md', 'json', 'yaml', 'txt')),
    content TEXT NOT NULL,
    -- Path relative to repo root, for replay / sync source-of-truth lookup.
    source_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (brand_id, content_key)
);

CREATE INDEX IF NOT EXISTS idx_brand_content_brand
    ON business.brand_content (brand_id);
