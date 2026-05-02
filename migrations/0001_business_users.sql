CREATE SCHEMA IF NOT EXISTS business;

CREATE TABLE IF NOT EXISTS business.users (
    id UUID PRIMARY KEY,
    auth_user_id UUID UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'client')),
    client_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_business_users_auth_user_id
    ON business.users (auth_user_id);
CREATE INDEX IF NOT EXISTS idx_business_users_role
    ON business.users (role);
