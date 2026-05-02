-- GTM-pipeline foundation §4.4 — per-org operator doctrine.
--
-- The doctrine markdown is the prose policy doc the operator authors against
-- (what to do, what not to do, why). The parameters JSONB is the structured
-- numeric override surface that gtm-sequence-definer reads at run start —
-- margin floor, capital outlay cap, per-piece guardrails, default touch
-- counts by audience size, model tier per step, gating mode default.
--
-- Per-org for forward compatibility (the post-payment pipeline could later
-- run for multiple Ben-owned operator workspaces), but in v0 only the
-- acq-eng row is populated.

CREATE TABLE IF NOT EXISTS business.org_doctrine (
    organization_id UUID PRIMARY KEY
        REFERENCES business.organizations(id) ON DELETE CASCADE,
    doctrine_markdown TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id UUID
);
