-- Per-piece direct-mail activation primitive:
-- `app/services/print_mail_activation.py` writes back-references
-- (_recipient_id, _channel_campaign_step_id, _membership_id) onto
-- `direct_mail_pieces.metadata` under reserved keys, instead of via the
-- FK-constrained dedicated columns. The metadata column has no GIN index
-- today (see migration 0011), so JSONB lookups on these reserved keys
-- would table-scan.
--
-- This index is a precondition for the analytics surface that joins
-- pieces to memberships / steps via the metadata path. The activation
-- service works without the index — the reads do not.

CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_metadata_gin
    ON direct_mail_pieces USING GIN (metadata);
