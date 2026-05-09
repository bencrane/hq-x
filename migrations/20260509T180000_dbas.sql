-- DBAs ("doing business as") — operator-facing brand identities under
-- a single legal entity. One legal_entity (e.g. Rare Structure LLC) can
-- have many DBAs (e.g. Engineered Demand, Acquisition Engineering,
-- Freight Expansion).
--
-- Intentionally not linked to business.organizations yet — orgs continue
-- to be the auth/tenant boundary. The DBA → org link can be added later
-- once we decide whether to keep both or collapse the concept.

CREATE TABLE IF NOT EXISTS business.dbas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_entity_id UUID NOT NULL
        REFERENCES business.legal_entities(id) ON DELETE RESTRICT,

    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    domain TEXT,

    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active',
        'inactive'
    )),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dbas_legal_entity
    ON business.dbas (legal_entity_id);
CREATE INDEX IF NOT EXISTS idx_dbas_active
    ON business.dbas (legal_entity_id)
    WHERE deleted_at IS NULL AND status = 'active';
