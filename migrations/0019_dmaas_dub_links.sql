-- Maps a Dub short link to the DMaaS / direct-mail artifact it represents.
-- One row per (dub_link_id). Joins are nullable: not every link is a mailer
-- (we'll mint links for emails too), and not every mailer has a link yet.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS dmaas_dub_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dub_link_id TEXT NOT NULL UNIQUE,            -- 'link_…' from Dub
    dub_external_id TEXT UNIQUE,                  -- our externalId if we set one
    dub_short_url TEXT NOT NULL,
    dub_domain TEXT NOT NULL,
    dub_key TEXT NOT NULL,
    destination_url TEXT NOT NULL,

    -- Optional joins. Filled in by the caller when minting in the context of
    -- a specific design / piece. Kept nullable so we can mint links eagerly.
    dmaas_design_id UUID REFERENCES dmaas_designs(id) ON DELETE SET NULL,
    direct_mail_piece_id UUID REFERENCES direct_mail_pieces(id) ON DELETE SET NULL,
    brand_id UUID REFERENCES business.brands(id) ON DELETE SET NULL,

    -- Free-form attribution context. Useful for "campaign_id", "send_batch_id"
    -- etc without needing a schema migration every time.
    attribution_context JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_design
    ON dmaas_dub_links (dmaas_design_id) WHERE dmaas_design_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_piece
    ON dmaas_dub_links (direct_mail_piece_id) WHERE direct_mail_piece_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dmaas_dub_links_brand
    ON dmaas_dub_links (brand_id) WHERE brand_id IS NOT NULL;
