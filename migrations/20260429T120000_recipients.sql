-- Recipients: channel-agnostic identity layer.
--
-- A recipient is a stable per-organization identity for whoever is being
-- contacted — a business, property, or person we know about. Pieces,
-- calls, and (future) emails all reference a recipient so the same
-- target across channels rolls up to a single entity.
--
-- Scope decisions (see PR description for rationale):
--   * Organization-scoped only. Brand is a contact-channel concern, not
--     identity.  No cross-org sharing — the same DOT in two orgs is two
--     recipient rows.
--   * Natural key is (organization_id, external_source, external_id).
--     Sources: 'fmcsa' (DOT number), 'nyc_re' (BBL), 'manual_upload'
--     (row hash), etc. The application layer normalizes external_id
--     before upsert.
--   * recipient_type is a top-level TEXT column ('business' | 'property'
--     | 'person') so audience-listing queries can filter on it.
--
-- The companion table channel_campaign_step_recipients is the audience-
-- targeting layer (which recipients are scheduled to be contacted by
-- which step, with status). Audience materialization happens at step
-- *configuration* time — before activation, before any pieces exist —
-- and creates rows here in 'pending' status.

-- ── 1. Recipients ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business.recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,

    recipient_type TEXT NOT NULL DEFAULT 'business'
        CHECK (recipient_type IN ('business', 'property', 'person', 'other')),

    -- Natural key: where this identity came from + the source's id for it.
    -- Required so we can dedupe and re-resolve.
    external_source TEXT NOT NULL,
    external_id TEXT NOT NULL,

    display_name TEXT,
    mailing_address JSONB NOT NULL DEFAULT '{}'::jsonb,
    phone TEXT,
    email TEXT,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    UNIQUE (organization_id, external_source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_recipients_org
    ON business.recipients (organization_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_recipients_org_type
    ON business.recipients (organization_id, recipient_type)
    WHERE deleted_at IS NULL;

-- ── 2. Step memberships (audience-targeting layer) ───────────────────────

CREATE TABLE IF NOT EXISTS business.channel_campaign_step_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_campaign_step_id UUID NOT NULL
        REFERENCES business.channel_campaign_steps(id) ON DELETE CASCADE,
    recipient_id UUID NOT NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,

    -- Denormalized for fast org-scoped queries without a step join.
    organization_id UUID NOT NULL
        REFERENCES business.organizations(id) ON DELETE RESTRICT,

    -- Lifecycle:
    --   pending    — audience materialized, step not yet activated
    --   scheduled  — step activated, send queued at provider
    --   sent       — provider confirmed acceptance / piece created
    --   failed     — provider rejected this recipient
    --   suppressed — recipient hit a suppression rule pre-send
    --   cancelled  — step or audience was cancelled before send
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'scheduled', 'sent', 'failed', 'suppressed', 'cancelled'
        )),

    scheduled_for TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    error_reason TEXT,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A recipient appears at most once per step. Audience modification
    -- before activation deletes pending rows and re-inserts.
    UNIQUE (channel_campaign_step_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_step_recipients_step_status
    ON business.channel_campaign_step_recipients
        (channel_campaign_step_id, status);
CREATE INDEX IF NOT EXISTS idx_step_recipients_recipient
    ON business.channel_campaign_step_recipients (recipient_id);
CREATE INDEX IF NOT EXISTS idx_step_recipients_org
    ON business.channel_campaign_step_recipients (organization_id);

-- ── 3. recipient_id on direct_mail_pieces ────────────────────────────────
--
-- Nullable to tolerate legacy rows. New writes from the Lob adapter must
-- populate this. A follow-up migration tightens to NOT NULL after audit.

ALTER TABLE direct_mail_pieces
    ADD COLUMN IF NOT EXISTS recipient_id UUID
        REFERENCES business.recipients(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_recipient
    ON direct_mail_pieces (recipient_id) WHERE recipient_id IS NOT NULL;
