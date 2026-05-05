-- Per-step content authoring discriminator.
--
-- Self-prospecting initiatives (see preceding migration) hand-author the
-- email body at the step level — same subject + body for every recipient,
-- with `{first_name}` as the only substitution token. This is distinct
-- from the existing partner-demand path where per-recipient creative is
-- LLM-generated at activation time and referenced via creative_ref.
--
-- Both modes coexist on the same channel_campaign_steps row shape:
--
--   content_mode='llm_per_recipient' (existing default)
--     → activation reads creative_ref → renders LLM-generated per-recipient
--       creative; channel_specific_config is provider-only config.
--   content_mode='manual' (new)
--     → activation reads channel_specific_config for the static content
--       (e.g. {subject, body_text, body_html} for email); applies a
--       single .replace('{first_name}', recipient.first_name) at send.
--
-- Future variants (cycle through N copies) extend the manual content
-- shape without touching the schema.

ALTER TABLE business.channel_campaign_steps
    ADD COLUMN IF NOT EXISTS content_mode TEXT NOT NULL DEFAULT 'llm_per_recipient'
        CHECK (content_mode IN ('llm_per_recipient', 'manual'));

CREATE INDEX IF NOT EXISTS idx_ccs_content_mode
    ON business.channel_campaign_steps (content_mode);

COMMENT ON COLUMN business.channel_campaign_steps.content_mode IS
    'llm_per_recipient: activation renders LLM-generated creative per recipient '
    '(via creative_ref). manual: activation reads channel_specific_config for '
    'operator-authored static content with {first_name} substitution.';
