-- Canonical SAM.gov Contract Opportunities bulk ingest — run audit log.
--
-- Written by the Modal compute worker (scripts/ingest/sam_opps_bulk_canonical.py)
-- via psycopg on terminal state (success or failure). Trigger.dev no longer
-- writes here — the compute that knows the true outcome owns the state row.
-- Lives in `ops.*` (the repo's ingest-audit convention), written by a direct
-- Postgres connection, so no PostgREST schema exposure is required.

create schema if not exists ops;

create table if not exists ops.sam_opps_canonical_runs (
    id             bigint generated always as identity primary key,
    feed           text        not null,
    rows_processed integer     not null default 0,
    status         text        not null,
    error          text,
    started_at     timestamptz not null,
    completed_at   timestamptz not null default now(),
    created_at     timestamptz not null default now()
);

create index if not exists sam_opps_canonical_runs_feed_completed_idx
    on ops.sam_opps_canonical_runs (feed, completed_at desc);
