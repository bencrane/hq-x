-- Migration 0023: channel_campaign_steps + direct_mail_pieces.step_id.
--
-- Introduces the step concept: a channel_campaign_step is one ordered touch
-- within a channel_campaign (e.g. "postcard at day 0", "letter at day 14").
-- For direct_mail, each step maps 1:1 to a Lob campaign object.
--
-- Hierarchy after this migration:
--   campaign  →  channel_campaign  →  channel_campaign_step  →  direct_mail_piece
--
-- (organization_id, brand_id, campaign_id) are denormalized onto the step row
-- for query convenience and webhook routing without joins. The application
-- layer keeps them consistent on insert (parent channel_campaign is the
-- source of truth).

-- ── 1. New table ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business.channel_campaign_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_campaign_id UUID NOT NULL
        REFERENCES business.channel_campaigns(id) ON DELETE CASCADE,
    campaign_id UUID NOT NULL
        REFERENCES business.campaigns(id) ON DELETE RESTRICT,
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL
        REFERENCES business.brands(id) ON DELETE RESTRICT,

    step_order INT NOT NULL CHECK (step_order >= 1),
    name TEXT,
    delay_days_from_previous INT NOT NULL DEFAULT 0
        CHECK (delay_days_from_previous >= 0),
    scheduled_send_at TIMESTAMPTZ,

    -- Per-channel polymorphic creative pointer. For channel='direct_mail',
    -- this is dmaas_designs.id; the application layer enforces brand-scope
    -- consistency (no DB-level FK because future channels will reference
    -- different tables).
    creative_ref UUID,

    channel_specific_config JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Provider's id for the step (Lob campaign id for direct_mail). NULL
    -- until activation creates it. Indexed for webhook lookup.
    external_provider_id TEXT,
    external_provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'scheduled', 'activating',
            'sent', 'failed', 'cancelled', 'archived'
        )),
    activated_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (channel_campaign_id, step_order)
);

CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_channel_campaign
    ON business.channel_campaign_steps (channel_campaign_id);
CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_campaign
    ON business.channel_campaign_steps (campaign_id);
CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_org
    ON business.channel_campaign_steps (organization_id);
CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_status
    ON business.channel_campaign_steps (status)
    WHERE status IN ('scheduled', 'activating');
CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_scheduled_send_at
    ON business.channel_campaign_steps (scheduled_send_at)
    WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS idx_channel_campaign_steps_external_provider_id
    ON business.channel_campaign_steps (external_provider_id)
    WHERE external_provider_id IS NOT NULL;

-- ── 2. Add channel_campaign_step_id to direct_mail_pieces ─────────────────
--
-- Nullable initially. Backfill is not required for legacy rows. New Lob
-- sends must populate it once the adapter is wired. A follow-up PR sets
-- NOT NULL after writes are confirmed.

ALTER TABLE direct_mail_pieces
    ADD COLUMN IF NOT EXISTS channel_campaign_step_id UUID
        REFERENCES business.channel_campaign_steps(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_step
    ON direct_mail_pieces (channel_campaign_step_id)
    WHERE channel_campaign_step_id IS NOT NULL;

-- ── 3. Backfill: every existing channel_campaign gets one default step. ──
--
-- Guarantees every channel_campaign has at least one step, simplifying
-- downstream code paths. step_order=1, delay=0, creative_ref pulled from
-- channel_campaigns.design_id (only meaningful for direct_mail; NULL for
-- other channels, which is fine — those will set creative_ref later).
--
-- Status mirrors the parent channel_campaign:
--   draft/scheduled/paused/failed → 'pending'
--   sending/sent                  → 'sent'
--   archived                      → 'archived'

INSERT INTO business.channel_campaign_steps (
    channel_campaign_id, campaign_id, organization_id, brand_id,
    step_order, delay_days_from_previous,
    creative_ref,
    status,
    metadata,
    created_at, updated_at
)
SELECT
    cc.id,
    cc.campaign_id,
    cc.organization_id,
    cc.brand_id,
    1,
    0,
    CASE WHEN cc.channel = 'direct_mail' THEN cc.design_id ELSE NULL END,
    CASE
        WHEN cc.status IN ('sending', 'sent') THEN 'sent'
        WHEN cc.status = 'archived'           THEN 'archived'
        ELSE 'pending'
    END,
    jsonb_build_object('backfill_source', '0023', 'parent_status', cc.status),
    cc.created_at,
    cc.updated_at
FROM business.channel_campaigns cc
WHERE NOT EXISTS (
    SELECT 1 FROM business.channel_campaign_steps s
    WHERE s.channel_campaign_id = cc.id
);

-- channel_campaigns.design_id is intentionally left in place. After the Lob
-- retrofit (this PR) lands and writes are confirmed flowing through steps,
-- a follow-up migration drops it.
