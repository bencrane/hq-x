-- Customer Activation v1: Leg 2 + Leg 3 supply-side outreach plumbing.
--
-- Pattern: when a demand-side partner pays for a supply-side audience
-- reservation, Ben hand-authors two gtm_initiatives (one per leg) under
-- this PR's new admin tile:
--
--   Leg 2 — Activation (Ben → supply-side audience members):
--     gtm_initiatives (kind='partner_demand', metadata.authoring_mode='manual')
--       └─ campaigns
--             └─ channel_campaigns
--                   └─ channel_campaign_steps (manual content per step)
--     Pacing: existing step_scheduler / dmaas.scheduled_step_activation
--     Trigger: operator clicks "Mark paid → launch" → status=active
--
--   Leg 3 — Intro (Ben → supply-side member who replied positive,
--   introducing them to the demand-side partner):
--     gtm_initiatives (kind='partner_demand', parent_initiative_id=Leg2.id,
--                      metadata.leg=3)
--       └─ campaigns
--             └─ channel_campaigns
--                   └─ channel_campaign_steps (1 step, content seeded
--                       from business.organizations.metadata.leg3_intro_template)
--     Trigger: positive_reply on Leg 2 — recorded in
--     business.email_reply_classifications, dispatched (eventually) by
--     intro.dispatch_pending_positives schedule.
--
-- Two additive surface changes:
--   1. gtm_initiatives.parent_initiative_id (Leg 3 → Leg 2 linkage)
--   2. business.email_reply_classifications (single-source-of-truth for
--      reply classification; whatever does the classification — agent,
--      EmailBison rule, manual psql — writes here; fire-intro reads here).
--
-- AI synthesis pipeline stays dormant for partner_demand-manual: the
-- operator simply doesn't fire /run-strategic-research or
-- /synthesize-strategy on these initiatives. The metadata.authoring_mode
-- flag is documentary only; no schema enforcement.

-- 1. parent_initiative_id column on gtm_initiatives.
ALTER TABLE business.gtm_initiatives
    ADD COLUMN IF NOT EXISTS parent_initiative_id UUID
        REFERENCES business.gtm_initiatives(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_gtm_initiatives_parent
    ON business.gtm_initiatives (parent_initiative_id)
    WHERE parent_initiative_id IS NOT NULL;

-- 2. email_reply_classifications: single source of truth for classification.
--
-- One row per business.email_messages row that received a reply (i.e.
-- email_messages.status = 'replied'). The classification is mutable
-- (re-classify via INSERT ... ON CONFLICT) but each row also carries
-- intro_fired_at + intro_email_message_id so the dispatcher can stay
-- idempotent on replay.
--
-- classification taxonomy:
--   'positive'      — explicit yes / interest; fire Leg 3 intro
--   'negative'      — explicit no / not now / not a fit
--   'unsubscribe'   — opt-out request; suppress + log
--   'question'      — non-committal; escalate to operator inbox
--   'auto_reply'    — OOO / bounce-bounce / other auto-generated
--   'unclassified'  — replied but classifier hasn't run / undecided
--
-- classified_by:
--   'manual'        — operator flipped a flag (psql, future admin UI)
--   'agent'         — managed agent classified
--   'emailbison'    — EmailBison's own positive-reply detection
--   'rule'          — keyword/rule-based heuristic in hq-x
CREATE TABLE IF NOT EXISTS business.email_reply_classifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_message_id UUID NOT NULL UNIQUE
        REFERENCES business.email_messages(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK (classification IN (
            'positive', 'negative', 'unsubscribe', 'question',
            'auto_reply', 'unclassified'
        )),
    classified_by TEXT NOT NULL
        CHECK (classified_by IN ('manual', 'agent', 'emailbison', 'rule')),
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    intro_fired_at TIMESTAMPTZ,
    intro_email_message_id UUID
        REFERENCES business.email_messages(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dispatcher's hot path: find positive replies that haven't been intro'd.
CREATE INDEX IF NOT EXISTS idx_erc_pending_positive
    ON business.email_reply_classifications (classified_at)
    WHERE classification = 'positive' AND intro_fired_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_erc_classification
    ON business.email_reply_classifications (classification);
