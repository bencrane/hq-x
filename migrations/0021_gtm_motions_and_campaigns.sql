-- Migration 0021: GTM motions + channel-typed campaigns
--
-- Two-layer outreach model:
--   * gtm_motions  — umbrella outreach motion, organization-scoped, channel-agnostic.
--   * campaigns    — channel-typed execution unit (direct_mail / email / voice_outbound /
--                     sms). Always belongs to exactly one motion.
--
-- The existing business.campaigns table is voice/SMS-flavored. It is renamed to
-- business.campaigns_legacy here; child tables (call_logs, sms_messages, voice_*)
-- have their composite (id, brand_id) FK dropped and their campaign_id columns
-- repointed at rows in the new business.campaigns table via a per-row mapping.
--
-- Inference rules during legacy backfill:
--   * channel  — 'sms' if only sms_messages reference the legacy id, otherwise
--                'voice_outbound' (default; ambiguous rows are flagged).
--   * provider — 'twilio' if assistant_substrate='twiml_ivr', else 'vapi' for
--                voice_outbound, 'twilio' for sms.
--   * org      — derived from brands.organization_id (NOT NULL after 0020).
--
-- A row in business.audit_events is written for every legacy campaign that was
-- referenced by both call_logs and sms_messages (channel inference ambiguous);
-- operators must reconcile those manually post-migration.

-- ── 1. New tables ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS business.gtm_motions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed', 'archived')),
    start_date DATE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gtm_motions_org
    ON business.gtm_motions (organization_id);
CREATE INDEX IF NOT EXISTS idx_gtm_motions_brand
    ON business.gtm_motions (brand_id);
CREATE INDEX IF NOT EXISTS idx_gtm_motions_active_paused
    ON business.gtm_motions (status)
    WHERE status IN ('active', 'paused');

-- ── 2. Drop composite (id, brand_id) FKs that reference the soon-to-be
--     renamed business.campaigns. We replace these with single-key FKs to
--     business.campaigns(id) after the new table exists; brand consistency
--     is enforced at the application layer.
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE voice_assistants
    DROP CONSTRAINT IF EXISTS voice_assistants_campaign_same_brand_fk;
ALTER TABLE voice_phone_numbers
    DROP CONSTRAINT IF EXISTS voice_phone_numbers_campaign_same_brand_fk;
ALTER TABLE call_logs
    DROP CONSTRAINT IF EXISTS call_logs_campaign_same_brand_fk;
ALTER TABLE transfer_territories
    DROP CONSTRAINT IF EXISTS transfer_territories_campaign_same_brand_fk;
ALTER TABLE sms_messages
    DROP CONSTRAINT IF EXISTS sms_messages_campaign_same_brand_fk;
ALTER TABLE voice_ai_campaign_configs
    DROP CONSTRAINT IF EXISTS vac_campaign_same_brand_fk;
ALTER TABLE voice_campaign_active_calls
    DROP CONSTRAINT IF EXISTS vcac_campaign_same_brand_fk;
ALTER TABLE voice_campaign_metrics
    DROP CONSTRAINT IF EXISTS vcm_campaign_same_brand_fk;
ALTER TABLE voice_callback_requests
    DROP CONSTRAINT IF EXISTS vcb_campaign_same_brand_fk;

-- ── 3. Rename existing voice/SMS-flavored campaigns table out of the way. ─

ALTER TABLE business.campaigns RENAME TO campaigns_legacy;
ALTER INDEX IF EXISTS uq_campaigns_id_brand RENAME TO uq_campaigns_legacy_id_brand;
ALTER INDEX IF EXISTS idx_campaigns_brand RENAME TO idx_campaigns_legacy_brand;
ALTER INDEX IF EXISTS idx_campaigns_partner RENAME TO idx_campaigns_legacy_partner;

-- ── 4. New channel-typed campaigns table. ─────────────────────────────────

CREATE TABLE IF NOT EXISTS business.campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gtm_motion_id UUID NOT NULL REFERENCES business.gtm_motions(id) ON DELETE RESTRICT,
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    channel TEXT NOT NULL
        CHECK (channel IN ('direct_mail', 'email', 'voice_outbound', 'sms')),
    provider TEXT NOT NULL
        CHECK (provider IN ('lob', 'emailbison', 'twilio', 'vapi', 'manual')),
    -- Cross-DB reference into data-engine-x; not FK-enforced.
    audience_spec_id UUID,
    audience_snapshot_count INT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'scheduled', 'sending', 'sent', 'paused', 'failed', 'archived')),
    start_offset_days INT NOT NULL DEFAULT 0,
    scheduled_send_at TIMESTAMPTZ,
    schedule_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- design_id references dmaas_designs (brand-scoped); required for direct_mail.
    design_id UUID REFERENCES dmaas_designs(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ,

    CONSTRAINT campaigns_design_required_for_direct_mail
        CHECK (channel != 'direct_mail' OR design_id IS NOT NULL OR status = 'archived')
);

CREATE INDEX IF NOT EXISTS idx_campaigns_motion
    ON business.campaigns (gtm_motion_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_org
    ON business.campaigns (organization_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_brand_v2
    ON business.campaigns (brand_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_channel_status
    ON business.campaigns (channel, status);
CREATE INDEX IF NOT EXISTS idx_campaigns_scheduled
    ON business.campaigns (scheduled_send_at)
    WHERE status = 'scheduled';

-- ── 5. Add campaign_id to direct_mail_pieces. (Existing rows pre-date the
--     campaign concept, so the column stays NULLABLE — no NOT NULL until a
--     follow-up after backfill confirms zero orphans on a real dataset.)
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE direct_mail_pieces
    ADD COLUMN IF NOT EXISTS campaign_id UUID
        REFERENCES business.campaigns(id) ON DELETE SET NULL;
ALTER TABLE direct_mail_pieces
    ADD COLUMN IF NOT EXISTS gtm_motion_id UUID
        REFERENCES business.gtm_motions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_campaign
    ON direct_mail_pieces (campaign_id) WHERE campaign_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_motion
    ON direct_mail_pieces (gtm_motion_id) WHERE gtm_motion_id IS NOT NULL;

-- ── 6. Backfill: legacy campaigns → motions + new campaigns + remap children.
--     Per-row mapping is built in a TEMP table (transaction-scoped). For each
--     legacy campaign we generate one motion id and one new-campaign id, and
--     infer (channel, provider) from the legacy row + which child tables
--     reference it.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TEMP TABLE _campaign_legacy_mapping (
    legacy_id UUID PRIMARY KEY,
    new_motion_id UUID NOT NULL,
    new_campaign_id UUID NOT NULL,
    organization_id UUID NOT NULL,
    brand_id UUID NOT NULL,
    inferred_channel TEXT NOT NULL,
    inferred_provider TEXT NOT NULL,
    legacy_name TEXT NOT NULL,
    legacy_status TEXT NOT NULL,
    has_voice_refs BOOLEAN NOT NULL,
    has_sms_refs BOOLEAN NOT NULL,
    needs_review BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

INSERT INTO _campaign_legacy_mapping (
    legacy_id, new_motion_id, new_campaign_id,
    organization_id, brand_id,
    inferred_channel, inferred_provider,
    legacy_name, legacy_status,
    has_voice_refs, has_sms_refs, needs_review,
    created_at, updated_at
)
SELECT
    cl.id,
    gen_random_uuid(),
    gen_random_uuid(),
    b.organization_id,
    cl.brand_id,
    -- inferred_channel
    CASE
        WHEN EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
             AND NOT EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id)
            THEN 'voice_outbound'
        WHEN NOT EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
             AND EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id)
            THEN 'sms'
        WHEN EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
             AND EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id)
            THEN 'voice_outbound'  -- ambiguous; flagged in needs_review
        ELSE 'voice_outbound'      -- no children; default to voice_outbound per legacy substrate
    END AS inferred_channel,
    -- inferred_provider
    CASE
        WHEN EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
             AND NOT EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id)
            THEN CASE WHEN cl.assistant_substrate = 'twiml_ivr' THEN 'twilio' ELSE 'vapi' END
        WHEN NOT EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
             AND EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id)
            THEN 'twilio'
        ELSE CASE WHEN cl.assistant_substrate = 'twiml_ivr' THEN 'twilio' ELSE 'vapi' END
    END AS inferred_provider,
    cl.name,
    cl.status,
    EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id),
    EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id),
    -- needs_review when both child types reference the same legacy id
    EXISTS (SELECT 1 FROM call_logs WHERE campaign_id = cl.id)
        AND EXISTS (SELECT 1 FROM sms_messages WHERE campaign_id = cl.id),
    cl.created_at,
    cl.updated_at
FROM business.campaigns_legacy cl
JOIN business.brands b ON b.id = cl.brand_id;

-- 6a. Insert one motion per legacy campaign.
INSERT INTO business.gtm_motions (
    id, organization_id, brand_id, name, status, metadata,
    created_at, updated_at
)
SELECT
    m.new_motion_id,
    m.organization_id,
    m.brand_id,
    'Legacy: ' || m.legacy_name,
    -- Map legacy status → motion status. Legacy used free-text 'active'
    -- by default; anything non-active rolls up to 'archived'.
    CASE m.legacy_status WHEN 'active' THEN 'active' ELSE 'archived' END,
    jsonb_build_object(
        'legacy_id', m.legacy_id,
        'inferred_channel', m.inferred_channel,
        'inferred_provider', m.inferred_provider,
        'needs_review', m.needs_review
    ),
    m.created_at,
    m.updated_at
FROM _campaign_legacy_mapping m;

-- 6b. Insert one new campaign per legacy campaign.
INSERT INTO business.campaigns (
    id, gtm_motion_id, organization_id, brand_id,
    name, channel, provider, status, metadata,
    created_at, updated_at
)
SELECT
    m.new_campaign_id,
    m.new_motion_id,
    m.organization_id,
    m.brand_id,
    m.legacy_name,
    m.inferred_channel,
    m.inferred_provider,
    -- Map legacy status into the new campaigns enum.
    CASE m.legacy_status WHEN 'active' THEN 'sending' ELSE 'archived' END,
    jsonb_build_object(
        'legacy_id', m.legacy_id,
        'needs_review', m.needs_review
    ),
    m.created_at,
    m.updated_at
FROM _campaign_legacy_mapping m;

-- 6c. Repoint child tables' campaign_id columns from legacy ids → new ids.
UPDATE call_logs
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE call_logs.campaign_id = m.legacy_id;

UPDATE sms_messages
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE sms_messages.campaign_id = m.legacy_id;

UPDATE voice_assistants
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_assistants.campaign_id = m.legacy_id;

UPDATE voice_phone_numbers
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_phone_numbers.campaign_id = m.legacy_id;

UPDATE transfer_territories
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE transfer_territories.campaign_id = m.legacy_id;

UPDATE voice_ai_campaign_configs
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_ai_campaign_configs.campaign_id = m.legacy_id;

UPDATE voice_campaign_active_calls
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_campaign_active_calls.campaign_id = m.legacy_id;

UPDATE voice_campaign_metrics
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_campaign_metrics.campaign_id = m.legacy_id;

UPDATE voice_callback_requests
SET campaign_id = m.new_campaign_id
FROM _campaign_legacy_mapping m
WHERE voice_callback_requests.campaign_id = m.legacy_id;

-- 6d. Audit-log every ambiguous legacy campaign for operator reconciliation.
INSERT INTO business.audit_events (
    organization_id, action, target_type, target_id, metadata
)
SELECT
    m.organization_id,
    'campaign.legacy_migrated.review_required',
    'campaign',
    m.new_campaign_id,
    jsonb_build_object(
        'legacy_id', m.legacy_id,
        'legacy_name', m.legacy_name,
        'inferred_channel', m.inferred_channel,
        'inferred_provider', m.inferred_provider,
        'has_voice_refs', m.has_voice_refs,
        'has_sms_refs', m.has_sms_refs
    )
FROM _campaign_legacy_mapping m
WHERE m.needs_review;

-- ── 7. Re-add child-table FKs against the new business.campaigns(id). ────

ALTER TABLE voice_assistants
    ADD CONSTRAINT voice_assistants_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

ALTER TABLE voice_phone_numbers
    ADD CONSTRAINT voice_phone_numbers_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

ALTER TABLE call_logs
    ADD CONSTRAINT call_logs_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

ALTER TABLE transfer_territories
    ADD CONSTRAINT transfer_territories_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

ALTER TABLE sms_messages
    ADD CONSTRAINT sms_messages_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

ALTER TABLE voice_ai_campaign_configs
    ADD CONSTRAINT voice_ai_campaign_configs_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE CASCADE;

ALTER TABLE voice_campaign_active_calls
    ADD CONSTRAINT voice_campaign_active_calls_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE CASCADE;

ALTER TABLE voice_campaign_metrics
    ADD CONSTRAINT voice_campaign_metrics_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE CASCADE;

ALTER TABLE voice_callback_requests
    ADD CONSTRAINT voice_callback_requests_campaign_fk
    FOREIGN KEY (campaign_id)
    REFERENCES business.campaigns(id) ON DELETE SET NULL;

-- business.campaigns_legacy is intentionally left in place. A follow-up
-- migration will drop it after all read paths are confirmed migrated.
