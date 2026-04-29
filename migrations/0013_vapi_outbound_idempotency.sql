-- Migration 0013: Idempotency ledger for Vapi-orchestrated outbound calls.
--
-- The ``initiate_vapi_call`` service requires every caller to pass a
-- per-call Idempotency-Key header. We persist that key + the resulting
-- call_log_id so retries with the same key return the same call (and
-- never trigger a duplicate Vapi POST /call charge).

CREATE TABLE IF NOT EXISTS vapi_call_idempotency (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id),
    idempotency_key TEXT NOT NULL,
    call_log_id UUID NOT NULL REFERENCES call_logs(id) ON DELETE CASCADE,
    vapi_call_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vapi_call_idempotency_brand_key
    ON vapi_call_idempotency(brand_id, idempotency_key);
