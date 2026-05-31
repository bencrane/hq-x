-- Migration 0002 (RECONSTRUCTED): legal_entities + companies.
--
-- Provenance / why this file was restored
-- ---------------------------------------
-- The `schema_migrations` ledger on prd (and dev) records `0002_companies.sql`
-- as applied (2026-04-29), but the file had gone missing from migrations/.
-- That made it an "orphan" ledger row: a fresh DB rebuilt from migrations/
-- alone could not reproduce prd. Worse, the missing file created
-- `business.legal_entities`, and a LATER in-repo migration
-- (20260509T180000_dbas.sql) declares an FK to it:
--
--     business.dbas.legal_entity_id -> business.legal_entities(id)
--
-- so a clean migrations/-only build actually FAILED at the dbas migration
-- with "relation business.legal_entities does not exist". This file is the
-- faithful reconstruction of what 0002 originally created, introspected
-- directly from the live prd schema. Restoring it under the original
-- filename re-points the ledger row at a real file (so prd, which already
-- has 0002 in its ledger, simply skips it) while letting a fresh build run
-- it and reproduce both tables.
--
-- Note on `companies`: it is reconstructed here for historical fidelity (0002
-- genuinely created it), but it is dead — 0 rows, no inbound FKs, no code
-- references — and ARCHITECTURE.md mandates a single-tenant model with no
-- companies table. It is therefore RETIRED by the forward migration
-- 20260531T000000_retire_companies_table.sql, which drops it. On a fresh
-- build the two files replay the table's true lifecycle: created here, then
-- retired. `legal_entities` is load-bearing and is kept.
--
-- Idempotent (IF NOT EXISTS): re-running against an already-migrated DB is a
-- safe no-op. Inline PK/UNIQUE/CHECK/FK definitions reproduce prd's
-- auto-generated constraint names (legal_entities_pkey,
-- legal_entities_owner_user_id_fkey, legal_entities_ach_account_type_check,
-- companies_pkey, companies_domain_key, companies_legal_entity_id_fkey).

-- legal_entities — the operator's legal/banking entity (e.g. the LLC that
-- owns the DBAs in 20260509T180000_dbas.sql). One row per real entity; holds
-- ACH/bank payout details. Created before `companies` because companies FKs
-- into it.
CREATE TABLE IF NOT EXISTS business.legal_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name TEXT NOT NULL,
    ein TEXT,

    address_line1 TEXT,
    address_line2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    country TEXT NOT NULL DEFAULT 'US',

    bank_name TEXT,
    ach_routing_number TEXT,
    ach_account_number TEXT,
    ach_account_type TEXT CHECK (ach_account_type IN ('checking', 'savings')),
    bank_address_line1 TEXT,
    bank_address_line2 TEXT,
    bank_city TEXT,
    bank_state TEXT,
    bank_zip TEXT,
    bank_country TEXT,

    stripe_account_id TEXT,

    owner_user_id UUID NOT NULL REFERENCES business.users(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- companies — see header. Reconstructed for fidelity; retired by
-- 20260531T000000_retire_companies_table.sql.
CREATE TABLE IF NOT EXISTS business.companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    legal_entity_id UUID NOT NULL REFERENCES business.legal_entities(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_companies_legal_entity
    ON business.companies (legal_entity_id);
