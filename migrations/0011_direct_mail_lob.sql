-- direct-mail (Lob) foundation
--
-- Collapses outbound-engine-x migrations 015/016/017/018/045/050 into one
-- single-tenant migration. No org_id, no per-org provider_configs FK, no
-- speculative companies/clients tables. Pieces link only to the
-- business.users row that created them.
--
-- This migration also creates `webhook_events` IF NOT EXISTS so the Lob
-- webhook receiver has somewhere to store inbound events. It is shaped to
-- be compatible with the existing EmailBison receiver in
-- app/webhooks/storage.py (provider_slug, event_key, event_type, status,
-- replay_count, payload).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Pieces: every postcard / letter / self-mailer / snap-pack / booklet we
-- ever ask Lob to mail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS direct_mail_pieces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_slug TEXT NOT NULL DEFAULT 'lob',
    external_piece_id VARCHAR(255) NOT NULL,
    piece_type VARCHAR(20) NOT NULL
        CHECK (piece_type IN ('postcard', 'letter', 'self_mailer', 'snap_pack', 'booklet')),
    status VARCHAR(40) NOT NULL DEFAULT 'unknown',
    send_date TIMESTAMPTZ,
    cost_cents INTEGER,
    deliverability VARCHAR(40),
    is_test_mode BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB,
    raw_payload JSONB,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (provider_slug, external_piece_id)
);

CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_type
    ON direct_mail_pieces (piece_type);
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_status
    ON direct_mail_pieces (status);
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_created_live
    ON direct_mail_pieces (created_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_type_status_created_live
    ON direct_mail_pieces (piece_type, status, created_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_created_by
    ON direct_mail_pieces (created_by_user_id) WHERE created_by_user_id IS NOT NULL;
-- Reports default to "live pieces only"; this index makes that fast.
CREATE INDEX IF NOT EXISTS idx_direct_mail_pieces_live_only
    ON direct_mail_pieces (created_at) WHERE deleted_at IS NULL AND is_test_mode = FALSE;

-- ---------------------------------------------------------------------------
-- Append-only event log: every Lob webhook that touches a piece writes
-- exactly one row here. Reconstruct piece history without grovelling
-- through webhook_events.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS direct_mail_piece_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    piece_id UUID NOT NULL REFERENCES direct_mail_pieces(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    previous_status VARCHAR(40),
    new_status VARCHAR(40),
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_id TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dmp_events_piece_received
    ON direct_mail_piece_events (piece_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_dmp_events_type
    ON direct_mail_piece_events (event_type);
CREATE INDEX IF NOT EXISTS idx_dmp_events_source_event_id
    ON direct_mail_piece_events (source_event_id) WHERE source_event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Suppression list: addresses we will not mail to. Keyed on a stable hash
-- of the normalized address (see app/direct_mail/addresses.py:normalize).
-- (address_hash, reason) is unique so the same address can be suppressed
-- for multiple reasons without dup rows for the same reason.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppressed_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    address_hash TEXT NOT NULL,
    address_line1 TEXT NOT NULL,
    address_line2 TEXT,
    address_city TEXT NOT NULL,
    address_state TEXT NOT NULL,
    address_zip TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_event_id TEXT,
    source_piece_id UUID REFERENCES direct_mail_pieces(id) ON DELETE SET NULL,
    notes TEXT,
    suppressed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (address_hash, reason)
);

CREATE INDEX IF NOT EXISTS idx_suppressed_addresses_hash
    ON suppressed_addresses (address_hash);
CREATE INDEX IF NOT EXISTS idx_suppressed_addresses_reason
    ON suppressed_addresses (reason);

-- ---------------------------------------------------------------------------
-- webhook_events already exists (created in an earlier hq-x migration with a
-- richer shape: org_id / company_id / brand_id / dispatch + identity
-- resolution columns / last_error / ingested_at). The Lob receiver only
-- needs three additions on top of that schema.
-- ---------------------------------------------------------------------------
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS schema_version TEXT;
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS reason_code TEXT;
