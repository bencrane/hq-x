-- Prototype draft table for the partner-platform Audience Composer tab.
-- Captures a MAGS thread session as a "draft reservation" keyed to
-- (organization_id, mags_thread_id). Distinct from
-- business.org_audience_reservations (which requires a frozen DEX
-- ops.audience_specs.id that the chat flow doesn't currently mint).
-- Promotion / merge to org_audience_reservations is a future scope cycle.

CREATE TABLE IF NOT EXISTS business.audience_composer_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE CASCADE,
    reserved_by_user_id UUID NOT NULL REFERENCES business.users(id) ON DELETE RESTRICT,
    mags_thread_id TEXT NOT NULL,
    mags_agent_id TEXT NOT NULL,
    audience_summary TEXT,
    audience_count_last_seen INTEGER,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','reserved','active','cancelled')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, mags_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_audience_composer_drafts_org
    ON business.audience_composer_drafts (organization_id);
CREATE INDEX IF NOT EXISTS idx_audience_composer_drafts_user
    ON business.audience_composer_drafts (reserved_by_user_id);
CREATE INDEX IF NOT EXISTS idx_audience_composer_drafts_thread
    ON business.audience_composer_drafts (mags_thread_id);
