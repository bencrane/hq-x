-- gtm_motions — taxonomy of GTM motions, replacing the unused
-- gtm_initiatives.initiative_type column.
--
-- The previous initiative_type column had a CHECK constraint of three
-- values (demand_side_acq | supply_side_acq | intro_match) but the
-- column had zero code references — it had fallen out of use. The
-- operator wants the taxonomy lifted into a proper lookup table with
-- slightly different naming:
--
--   demand-side-acq                                            (was demand_side_acq)
--   supply-side-opt-in                                          (was supply_side_acq)
--   intro-match-demand-side-partner-and-supply-side-opted-in    (was intro_match)
--
-- All existing gtm_initiatives rows backfill to demand-side-acq per
-- the existing initiative_type data (all 5 rows currently labeled
-- demand_side_acq — verified pre-migration).

CREATE TABLE IF NOT EXISTS business.gtm_motions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO business.gtm_motions (slug, name, description) VALUES
    (
        'demand-side-acq',
        'Demand-side acquisition',
        'Operator-launched outreach to demand-side partners to opt them into the prepaid lead-transfer model.'
    ),
    (
        'supply-side-opt-in',
        'Supply-side opt-in',
        'Post-payment outreach to members of the audience the demand-side partner reserved, getting them to opt into an introduction.'
    ),
    (
        'intro-match-demand-side-partner-and-supply-side-opted-in',
        'Intro match: demand-side partner ↔ supply-side opted-in member',
        'The introduction itself, fired when a supply-side member responds positively to the supply-side opt-in outreach.'
    )
ON CONFLICT (slug) DO NOTHING;


-- Add the new FK column on gtm_initiatives.
ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS gtm_motion_id UUID
        REFERENCES business.gtm_motions(id);

-- Backfill: all existing rows → demand-side-acq.
UPDATE business.gtm_initiatives
SET gtm_motion_id = (
    SELECT id FROM business.gtm_motions WHERE slug = 'demand-side-acq'
)
WHERE gtm_motion_id IS NULL;

-- Drop the old check constraint + column.
ALTER TABLE business.gtm_initiatives
    DROP CONSTRAINT IF EXISTS gtm_initiatives_initiative_type_check;
ALTER TABLE business.gtm_initiatives
    DROP COLUMN IF EXISTS initiative_type;

-- Index for filtering by motion (dashboard / orchestrator routing).
CREATE INDEX IF NOT EXISTS idx_gtm_initiatives_motion
    ON business.gtm_initiatives (gtm_motion_id)
    WHERE gtm_motion_id IS NOT NULL;
