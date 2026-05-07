-- Cluster 3 observability — heartbeat log, reconciliation log, alert log.
--
-- Three tables, all additive, all `IF NOT EXISTS`:
--
--   1. cluster3_heartbeat_log — one row per synthetic-heartbeat run.
--      Lets the dashboard answer "is the chain alive right now."
--
--   2. cluster3_reconciliation_log — one row per reconciliation sweep.
--      Records how many EB replies the sweep found that we'd missed.
--      Non-zero = a webhook was dropped and the sweep recovered it.
--
--   3. cluster3_alerts — one row per outbound alert (Telegram / log).
--      Audit trail so the operator can see what they were told about
--      even after Telegram retention windows expire.

CREATE TABLE IF NOT EXISTS business.cluster3_heartbeat_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'pass', 'fail')),
    duration_ms INTEGER,
    classification_id UUID,
    lead_transfer_id UUID,
    intro_email_message_id UUID,
    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_c3_hb_started
    ON business.cluster3_heartbeat_log (started_at DESC);


CREATE TABLE IF NOT EXISTS business.cluster3_reconciliation_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'pass', 'fail')),
    duration_ms INTEGER,
    eb_replies_scanned INTEGER NOT NULL DEFAULT 0,
    eb_replies_already_processed INTEGER NOT NULL DEFAULT 0,
    eb_replies_backfilled INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_c3_recon_started
    ON business.cluster3_reconciliation_log (started_at DESC);


CREATE TABLE IF NOT EXISTS business.cluster3_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivered_to TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    delivery_failures JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_c3_alerts_fired
    ON business.cluster3_alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_c3_alerts_severity
    ON business.cluster3_alerts (severity, fired_at DESC);


-- Track which lead_transfers we've attempted to recover from stuck-queue
-- state. Adding a small set of fields onto lead_transfers via additive
-- column adds — recover_attempt_count + last_recover_attempt_at — so the
-- recovery sweep can back off after N attempts.
ALTER TABLE business.lead_transfers
    ADD COLUMN IF NOT EXISTS recover_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE business.lead_transfers
    ADD COLUMN IF NOT EXISTS last_recover_attempt_at TIMESTAMPTZ;

-- For finding stuck queued rows fast.
CREATE INDEX IF NOT EXISTS idx_lt_stuck_queued
    ON business.lead_transfers (queued_at)
    WHERE status = 'queued';


-- Allow 'pending_review' status on lead_transfers (verdict gate parks
-- intros there for operator review). Drop+recreate the CHECK constraint.
ALTER TABLE business.lead_transfers
    DROP CONSTRAINT IF EXISTS lead_transfers_status_check;
ALTER TABLE business.lead_transfers
    ADD CONSTRAINT lead_transfers_status_check
    CHECK (status IN (
        'queued', 'sent', 'failed', 'deferred_capped', 'cancelled',
        'pending_review'
    ));

-- Allow the unique-index against pending_review too (gates re-dispatch
-- on the same classification while the row is parked).
DROP INDEX IF EXISTS uniq_lt_classification_active;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_lt_classification_active
    ON business.lead_transfers (email_reply_classification_id)
    WHERE email_reply_classification_id IS NOT NULL
      AND status IN ('queued', 'sent', 'pending_review');

-- email_messages.status enum is already permissive (TEXT with no CHECK
-- on this app's branch), so 'pending_review' value is accepted as-is.
-- If a CHECK is added later, it must include 'pending_review'.
