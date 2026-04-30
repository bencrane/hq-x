-- Migration 0022: rename the campaigns hierarchy.
--
--   business.gtm_motions → business.campaigns          (the umbrella)
--   business.campaigns   → business.channel_campaigns  (the channel-typed
--                                                       execution unit)
--
-- Plus: drop business.campaigns_legacy. The post-ship audit on 2026-04-29
-- confirmed zero rows in legacy + zero needs_review audit_events on the
-- hq-x project (no production campaigns existed when 0021 ran), so the
-- legacy table has no recovery value.
--
-- Column-level renames so reads on child tables make sense under the new
-- terminology:
--   * <child_table>.campaign_id          → channel_campaign_id
--     (call_logs, sms_messages, voice_assistants, voice_phone_numbers,
--      transfer_territories, voice_ai_campaign_configs,
--      voice_campaign_active_calls, voice_campaign_metrics,
--      voice_callback_requests, direct_mail_pieces)
--   * direct_mail_pieces.gtm_motion_id   → campaign_id  (umbrella)
--
-- After this migration, "campaign" means the umbrella outreach effort and
-- "channel campaign" means the per-channel execution under it.

-- ── 1. Drop the legacy table. -----------------------------------------------
--
-- 0021 left this in place "after read paths are confirmed migrated"; they
-- have been (#18 updated every caller). Verified empty before drop.
DROP TABLE IF EXISTS business.campaigns_legacy;

-- ── 2. Rename the channel-typed table out of the way. ---------------------

ALTER TABLE business.campaigns RENAME TO channel_campaigns;

ALTER INDEX IF EXISTS idx_campaigns_motion         RENAME TO idx_channel_campaigns_campaign;
ALTER INDEX IF EXISTS idx_campaigns_org            RENAME TO idx_channel_campaigns_org;
ALTER INDEX IF EXISTS idx_campaigns_brand_v2       RENAME TO idx_channel_campaigns_brand;
ALTER INDEX IF EXISTS idx_campaigns_channel_status RENAME TO idx_channel_campaigns_channel_status;
ALTER INDEX IF EXISTS idx_campaigns_scheduled      RENAME TO idx_channel_campaigns_scheduled;

-- The CHECK constraint name drifts to its old name. Rename for clarity.
ALTER TABLE business.channel_campaigns
    RENAME CONSTRAINT campaigns_design_required_for_direct_mail
    TO channel_campaigns_design_required_for_direct_mail;

-- ── 3. Rename the umbrella table into the freed-up `campaigns` name. ------

ALTER TABLE business.gtm_motions RENAME TO campaigns;

ALTER INDEX IF EXISTS idx_gtm_motions_org           RENAME TO idx_campaigns_org;
ALTER INDEX IF EXISTS idx_gtm_motions_brand         RENAME TO idx_campaigns_brand;
ALTER INDEX IF EXISTS idx_gtm_motions_active_paused RENAME TO idx_campaigns_active_paused;

-- ── 4. Rename the parent FK column on channel_campaigns. -----------------

ALTER TABLE business.channel_campaigns
    RENAME COLUMN gtm_motion_id TO campaign_id;

-- ── 5. Rename the campaign_id columns on every child table to
--     channel_campaign_id (since they reference channel_campaigns now).
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE call_logs              RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE sms_messages           RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_assistants       RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_phone_numbers    RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE transfer_territories   RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_ai_campaign_configs
    RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_campaign_active_calls
    RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_campaign_metrics RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE voice_callback_requests
    RENAME COLUMN campaign_id TO channel_campaign_id;

-- direct_mail_pieces is a swap, not just a rename:
--   * old `campaign_id`     (channel-typed) → `channel_campaign_id`
--   * old `gtm_motion_id`   (umbrella)      → `campaign_id`
ALTER TABLE direct_mail_pieces
    RENAME COLUMN campaign_id TO channel_campaign_id;
ALTER TABLE direct_mail_pieces
    RENAME COLUMN gtm_motion_id TO campaign_id;

-- ── 6. Rename the indexes that referenced the old column names. ----------

ALTER INDEX IF EXISTS idx_direct_mail_pieces_campaign
    RENAME TO idx_direct_mail_pieces_channel_campaign;
ALTER INDEX IF EXISTS idx_direct_mail_pieces_motion
    RENAME TO idx_direct_mail_pieces_campaign;

-- ── 7. Rename FK constraints on child tables for clarity. ----------------
--
-- Postgres preserves the FK target across the parent rename automatically
-- (FKs reference table OIDs, not names), so the constraints below still
-- enforce the right relationship — only the names are misleading.

ALTER TABLE voice_assistants
    RENAME CONSTRAINT voice_assistants_campaign_fk
    TO voice_assistants_channel_campaign_fk;
ALTER TABLE voice_phone_numbers
    RENAME CONSTRAINT voice_phone_numbers_campaign_fk
    TO voice_phone_numbers_channel_campaign_fk;
ALTER TABLE call_logs
    RENAME CONSTRAINT call_logs_campaign_fk
    TO call_logs_channel_campaign_fk;
ALTER TABLE transfer_territories
    RENAME CONSTRAINT transfer_territories_campaign_fk
    TO transfer_territories_channel_campaign_fk;
ALTER TABLE sms_messages
    RENAME CONSTRAINT sms_messages_campaign_fk
    TO sms_messages_channel_campaign_fk;
ALTER TABLE voice_ai_campaign_configs
    RENAME CONSTRAINT voice_ai_campaign_configs_campaign_fk
    TO voice_ai_campaign_configs_channel_campaign_fk;
ALTER TABLE voice_campaign_active_calls
    RENAME CONSTRAINT voice_campaign_active_calls_campaign_fk
    TO voice_campaign_active_calls_channel_campaign_fk;
ALTER TABLE voice_campaign_metrics
    RENAME CONSTRAINT voice_campaign_metrics_campaign_fk
    TO voice_campaign_metrics_channel_campaign_fk;
ALTER TABLE voice_callback_requests
    RENAME CONSTRAINT voice_callback_requests_campaign_fk
    TO voice_callback_requests_channel_campaign_fk;
