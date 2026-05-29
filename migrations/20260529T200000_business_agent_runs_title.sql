-- Add a renameable, operator-facing title to business.agent_runs so the
-- /agent chat sidebar can list + rename past sessions server-side (durable,
-- cross-device) — replacing the localStorage index. Title is NULL for rows
-- created before this; the list endpoint falls back to LEFT(initial_message,
-- 80) for display.
--
-- Forward-only, idempotent per hq-x convention.

ALTER TABLE business.agent_runs ADD COLUMN IF NOT EXISTS title TEXT;
