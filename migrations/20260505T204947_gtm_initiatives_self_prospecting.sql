-- Self-prospecting initiative kind.
--
-- The original gtm_initiatives table modeled only the demand-side-partner
-- flow: a paying partner reserves a slice of one of Ben's audiences,
-- partner_id + partner_contract_id are mandatory, and the AI synthesis
-- pipeline (strategic-context research → strategy synthesis →
-- materializers) drives the campaign shape.
--
-- This migration adds a second kind, `self_prospecting`, where Ben himself
-- is the org doing outreach (e.g. Freight Expansion prospecting freight
-- brokers). There is no demand-side partner, no contract, and no AI
-- synthesis — the operator hand-builds channel_campaigns + steps via the
-- Initiative Composer admin page and presses Launch when ready.
--
-- Reuse rationale: the activation/scheduler/webhook plumbing on
-- channel_campaigns + step_scheduler is identical for both kinds. The
-- only difference is who fills the rows. So we extend the existing
-- gtm_initiatives table rather than fork.
--
-- Schema changes:
--   1. New `kind` column (NOT NULL, default 'partner_demand' for
--      backward compat with existing rows).
--   2. partner_id and partner_contract_id become nullable.
--   3. CHECK constraint enforces (kind, partner FK presence) coupling:
--      partner_demand     → partner_id + partner_contract_id NOT NULL
--      self_prospecting   → partner_id + partner_contract_id NULL

ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'partner_demand'
        CHECK (kind IN ('partner_demand', 'self_prospecting'));

ALTER TABLE business.gtm_initiatives
    ALTER COLUMN partner_id DROP NOT NULL;

ALTER TABLE business.gtm_initiatives
    ALTER COLUMN partner_contract_id DROP NOT NULL;

ALTER TABLE business.gtm_initiatives
    ADD CONSTRAINT chk_gtm_kind_partner_coupling CHECK (
        (kind = 'partner_demand'
            AND partner_id IS NOT NULL
            AND partner_contract_id IS NOT NULL)
        OR
        (kind = 'self_prospecting'
            AND partner_id IS NULL
            AND partner_contract_id IS NULL)
    );

CREATE INDEX IF NOT EXISTS idx_gtm_kind
    ON business.gtm_initiatives (kind);

COMMENT ON COLUMN business.gtm_initiatives.kind IS
    'partner_demand: paying-partner reserves a slice; AI synthesizes campaign. '
    'self_prospecting: Ben prospects on his own behalf; operator hand-builds '
    'channel_campaigns + steps via the Initiative Composer.';
