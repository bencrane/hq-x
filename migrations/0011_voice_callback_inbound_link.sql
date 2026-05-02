-- Migration 0011: §7.5 inbound-SMS reply matching for voice_callback_requests
--
-- When an inbound SMS arrives whose `from_number` matches an open
-- voice_callback_requests row (status='scheduled', not deleted, within
-- the last 48h), we link the inbound SMS to that callback row so the
-- operator can see at a glance that the lead acknowledged the reminder.
--
-- Per directive §7.5 + §10 #3: we record the link only — no LLM-driven
-- reschedule parsing in Phase 1 (deferred to v2).

ALTER TABLE voice_callback_requests
    ADD COLUMN IF NOT EXISTS last_inbound_sms_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_inbound_sms_sid TEXT;

CREATE INDEX IF NOT EXISTS idx_vcb_from_recent
    ON voice_callback_requests(brand_id, customer_number, created_at)
    WHERE deleted_at IS NULL AND status = 'scheduled';
