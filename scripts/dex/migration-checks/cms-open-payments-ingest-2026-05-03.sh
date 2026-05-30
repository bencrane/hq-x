#!/usr/bin/env bash
# Verification harness for directive: cms-open-payments-ingest-2026-05-03
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (2):
#   s1  migration  apps/data-engine-x/supabase/migrations/20260503160000_cms_open_payments_source_tables.sql
#                  Creates: entities.source_cms_open_payments_general
#                           entities.source_cms_open_payments_research
#                           entities.source_cms_open_payments_ownership
#                           ops.cms_open_payments_ingest_runs
#   s2  code       apps/data-engine-x/scripts/run_cms_open_payments_ingest.py
#
# Apply-mechanism (validator-confirmed): MANUAL post-merge / pre-PR via
#   doppler run -p hq-all -c prd -- bash -c \
#     'psql "$DEX_DB_URL_DIRECT" -f apps/data-engine-x/supabase/migrations/20260503160000_cms_open_payments_source_tables.sql'
# Executor pre-applies BEFORE opening the PR. CI does NOT apply migrations
# (no .github/workflows/ step, no supabase/config.toml, no Makefile target).
# By the time the PR is opened, all four s1 table-existence checks below MUST pass.
#
# Doppler shell convention (apps/data-engine-x/CLAUDE.md § "Doppler Shell Gotcha"):
#   single-quote the inner shell so $-expansion defers to runtime.
#   doppler run -p hq-all -c prd -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "..."'
#   DEX_DB_URL_DIRECT for DDL/DROP/CONCURRENTLY; DEX_DB_URL_POOLED for pooled reads.
#
# Usage: ./cms-open-payments-ingest-2026-05-03.sh [--repo bencrane/hq-all] [--surface s1|s2]

set -euo pipefail

REPO_FILTER=""
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

WORKTREE="${DEX_WORKTREE_DIR:-/Users/benjamincrane/hq-all/.claude/worktrees/pedantic-pascal-dd9a57}"
APP_DIR="${DEX_APP_DIR:-$WORKTREE/apps/data-engine-x}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

REMOTE=$(git -C "$WORKTREE" remote get-url origin 2>&1 || echo "MISSING")
if [[ "$REMOTE" != *"bencrane/hq-all"* ]]; then
  echo "FAIL: $WORKTREE origin is '$REMOTE' — expected bencrane/hq-all" >&2
  exit 1
fi

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

echo "==> Verifying CMS Open Payments source tables + ingest script (filter: ${REPO_FILTER:-all})"

# ---------------------------------------------------------------------------- #
# s1: migration — four tables created by
#     20260503160000_cms_open_payments_source_tables.sql
# Asserts:
#   - entities.source_cms_open_payments_general exists; PK = record_id
#   - entities.source_cms_open_payments_research exists; PK = record_id
#   - entities.source_cms_open_payments_ownership exists; PK = record_id
#   - ops.cms_open_payments_ingest_runs       exists; UNIQUE INDEX on
#       (run_id, feed_name, program_year, attempt) present;
#       feed_name CHECK accepts {general, research, ownership}
#   - On each entities.source_*: index on the NPI column
#     (covered_recipient_npi for general/research; physician_npi for ownership)
#   - On each entities.source_*: index on program_year
# ---------------------------------------------------------------------------- #
S1_CHECK='
  cd "$APP_DIR" && \
  doppler run -p hq-all -c prd -- bash -c '"'"'
    set -e
    for t in source_cms_open_payments_general source_cms_open_payments_research source_cms_open_payments_ownership; do
      EXISTS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
        SELECT 1 FROM information_schema.tables
          WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
            AND table_name   = '"'"'"'"'"'"'"'"'$t'"'"'"'"'"'"'"'"';
      ")
      [ "$EXISTS" = "1" ] || { echo "entities.$t missing"; exit 1; }
      PK=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
        SELECT a.attname FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
          WHERE i.indrelid = ('"'"'"'"'"'"'"'"'entities.'"'"'"'"'"'"'"'"' || '"'"'"'"'"'"'"'"'$t'"'"'"'"'"'"'"'"')::regclass
            AND i.indisprimary;
      ")
      [ "$PK" = "record_id" ] || { echo "entities.$t expected PK record_id, got $PK"; exit 1; }
      PY_IDX=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
        SELECT count(*) FROM pg_indexes
          WHERE schemaname = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
            AND tablename  = '"'"'"'"'"'"'"'"'$t'"'"'"'"'"'"'"'"'
            AND indexdef   ILIKE '"'"'"'"'"'"'"'"'%(program_year%'"'"'"'"'"'"'"'"';
      ")
      [ "$PY_IDX" -ge "1" ] || { echo "entities.$t missing program_year index"; exit 1; }
    done

    for pair in "source_cms_open_payments_general:covered_recipient_npi" "source_cms_open_payments_research:covered_recipient_npi" "source_cms_open_payments_ownership:physician_npi"; do
      tbl="${pair%%:*}"; col="${pair##*:}"
      NPI_IDX=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
        SELECT count(*) FROM pg_indexes
          WHERE schemaname = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
            AND tablename  = '"'"'"'"'"'"'"'"'$tbl'"'"'"'"'"'"'"'"'
            AND indexdef   ILIKE '"'"'"'"'"'"'"'"'%('"'"'"'"'"'"'"'"' || '"'"'"'"'"'"'"'"'$col'"'"'"'"'"'"'"'"' || '"'"'"'"'"'"'"'"'%'"'"'"'"'"'"'"'"';
      ")
      [ "$NPI_IDX" -ge "1" ] || { echo "entities.$tbl missing index on $col"; exit 1; }
    done

    RUNS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'cms_open_payments_ingest_runs'"'"'"'"'"'"'"'"';
    ")
    [ "$RUNS" = "1" ] || { echo "ops.cms_open_payments_ingest_runs missing"; exit 1; }

    UNIQ=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM pg_indexes
        WHERE schemaname = '"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"'
          AND tablename  = '"'"'"'"'"'"'"'"'cms_open_payments_ingest_runs'"'"'"'"'"'"'"'"'
          AND indexdef ILIKE '"'"'"'"'"'"'"'"'CREATE UNIQUE INDEX%(run_id, feed_name, program_year, attempt)%'"'"'"'"'"'"'"'"';
    ")
    [ "$UNIQ" = "1" ] || { echo "ops.cms_open_payments_ingest_runs missing UNIQUE INDEX (run_id, feed_name, program_year, attempt)"; exit 1; }

    FEEDS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = '"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"'
          AND t.relname = '"'"'"'"'"'"'"'"'cms_open_payments_ingest_runs'"'"'"'"'"'"'"'"'
          AND c.contype = '"'"'"'"'"'"'"'"'c'"'"'"'"'"'"'"'"'
          AND pg_get_constraintdef(c.oid) ILIKE '"'"'"'"'"'"'"'"'%feed_name%'"'"'"'"'"'"'"'"';
    ")
    case "$FEEDS" in
      *general*research*ownership*) : ;;
      *) echo "ops.cms_open_payments_ingest_runs feed_name CHECK missing one of {general, research, ownership}; got: $FEEDS"; exit 1 ;;
    esac

    echo "s1 ok"
  '"'"'
'

# ---------------------------------------------------------------------------- #
# s2: code — apps/data-engine-x/scripts/run_cms_open_payments_ingest.py
# Asserts:
#   - file exists and parses
#   - has def main
#   - has def resolve_metastore_url (or equivalent metastore-probe helper)
#   - references openpaymentsdata.cms.gov metastore endpoint
#   - references each of: general, research, ownership feeds
#   - has --skip-if-unchanged and --recon-only flags
#   - upsert path: ON CONFLICT (record_id) clause present
# Idempotency stress test deliberately omitted — Open Payments full files are
# multi-GB and the executor exercises the loader once manually post-merge.
# ---------------------------------------------------------------------------- #
S2_CHECK='
  SCRIPT="$APP_DIR/scripts/run_cms_open_payments_ingest.py"
  test -f "$SCRIPT" || { echo "script missing: $SCRIPT"; exit 1; }
  python3 -c "import ast; ast.parse(open('"'"'$SCRIPT'"'"').read())" || { echo "syntax error: $SCRIPT"; exit 1; }
  grep -q "def main" "$SCRIPT" || { echo "missing def main"; exit 1; }
  grep -qE "def resolve_(metastore_url|open_payments_url)" "$SCRIPT" || { echo "missing def resolve_metastore_url / resolve_open_payments_url"; exit 1; }
  grep -q "openpaymentsdata.cms.gov/api/1/metastore" "$SCRIPT" || { echo "missing metastore endpoint reference"; exit 1; }
  grep -q "general"   "$SCRIPT" || { echo "missing general feed reference"; exit 1; }
  grep -q "research"  "$SCRIPT" || { echo "missing research feed reference"; exit 1; }
  grep -q "ownership" "$SCRIPT" || { echo "missing ownership feed reference"; exit 1; }
  grep -q -- "--skip-if-unchanged" "$SCRIPT" || { echo "missing --skip-if-unchanged flag"; exit 1; }
  grep -q -- "--recon-only"        "$SCRIPT" || { echo "missing --recon-only flag"; exit 1; }
  grep -q "ON CONFLICT (record_id)" "$SCRIPT" || { echo "missing ON CONFLICT (record_id) clause"; exit 1; }
  echo "s2 ok"
'

run_surface "s1" "bencrane/hq-all" "$S1_CHECK"
run_surface "s2" "bencrane/hq-all" "$S2_CHECK"

echo "==> All filtered surfaces passed."
