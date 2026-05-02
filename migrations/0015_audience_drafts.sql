-- Migration 0014: audience_drafts — user-owned saved customizations of a
-- DEX (data-engine-x) audience template.
--
-- HQ-X is the source of truth for saved drafts. DEX owns the templates
-- (catalog + form schemas + defaults) and the live preview/query surface.
-- Drafts reference templates by slug only — no cross-service FK.
--
-- Intentionally narrow: no proposal/agreement lifecycle, no lead/company
-- association, no payment hook. See companion executor directive.

CREATE TABLE IF NOT EXISTS business.audience_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Supabase auth.users.id of the operator who owns this draft. No FK
    -- across to auth schema; we just store the uuid.
    created_by_user_id UUID NOT NULL,

    name TEXT NOT NULL,

    -- DEX template slug (e.g. "motor-carriers-new-entrants-90d"). Free text.
    audience_template_slug TEXT NOT NULL,

    -- DEX endpoint to call for live preview. Cached at save time so HQ-X can
    -- hand it back without re-fetching the template from DEX.
    source_endpoint TEXT NOT NULL,

    -- Only the keys the operator changed from the template's defaults.
    filter_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Frozen merged snapshot (template.default_filters ⊕ filter_overrides),
    -- supplied by the frontend at save time. Does NOT auto-update if upstream
    -- template defaults change.
    resolved_filters JSONB NOT NULL,

    -- Optional UI breadcrumb: the total_matched count visible when saved.
    last_preview_total_matched INTEGER,
    last_preview_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audience_drafts_user_created
    ON business.audience_drafts (created_by_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audience_drafts_template_slug
    ON business.audience_drafts (audience_template_slug);
