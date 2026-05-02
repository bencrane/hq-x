-- Track which Dub webhooks we've registered programmatically per
-- environment, so we can reconcile across deploys without
-- click-opsing the Dub dashboard. The plaintext signing secret is
-- never stored — only a sha256 hash for audit / rotation tracking.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS dub_webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dub_webhook_id TEXT NOT NULL UNIQUE,        -- 'wh_…' from Dub
    name TEXT NOT NULL,
    receiver_url TEXT NOT NULL,
    secret_hash TEXT,                           -- sha256 of secret for audit (never store plaintext)
    triggers JSONB NOT NULL DEFAULT '[]'::jsonb,-- ['link.clicked','lead.created','sale.created']
    environment TEXT NOT NULL,                  -- 'dev'|'stg'|'prd' — match APP_ENV
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dub_webhooks_env
    ON dub_webhooks (environment) WHERE is_active;
