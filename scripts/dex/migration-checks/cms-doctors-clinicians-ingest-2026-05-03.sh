#!/usr/bin/env bash
# Verification harness for directive: cms-doctors-clinicians-ingest-2026-05-03
# Filled by AUDIT subagent on 2026-05-03.
#
# Surfaces (post-audit, 2 active):
#   s1 migration  apps/data-engine-x/supabase/migrations/20260503160000_doctors_clinicians_source_tables.sql
#   s2 code       apps/data-engine-x/scripts/run_doctors_clinicians_ingest.py
#
# Surfaces OMITTED:
#   s3 config     no schedule registered (manual-first; follow-up directive wires Trigger.dev or pg_cron)
#   s4 deploy     Railway data-engine-x project deletedAt=2026-05-05; migrations apply via manual psql
#
# Usage: ./cms-doctors-clinicians-ingest-2026-05-03.sh [--repo hq-all] [--surface s1|s2]

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

WORKTREE="${DEX_WORKTREE_DIR:-/Users/benjamincrane/hq-all/.claude/worktrees/zen-diffie-9282e2}"
APP_DIR="${DEX_APP_DIR:-$WORKTREE/apps/data-engine-x}"
DOPPLER_DIR="${DEX_DOPPLER_DIR:-/Users/benjamincrane/hq-all/apps/data-engine-x}"
SCRIPT_PATH="$APP_DIR/scripts/run_doctors_clinicians_ingest.py"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
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

# --- s1: migration --------------------------------------------------------- #
# 4 conditions: source table exists, runs table exists, raw_source_row column,
# PK is npi. DDL-safe DEX_DB_URL_DIRECT (pgbouncer transaction-mode at the
# pooled URL blocks DDL/system catalog lookups). doppler run + bash -c per
# CLAUDE.md's Doppler shell gotcha.

S1_VERIFY="( cd '$DOPPLER_DIR' && doppler run -- bash -c \"psql \\\"\\\$DEX_DB_URL_DIRECT\\\" -tAc \\\"SELECT 1 FROM pg_tables WHERE schemaname='entities' AND tablename='source_doctors_clinicians'\\\"\" ) | grep -q 1 \
  && ( cd '$DOPPLER_DIR' && doppler run -- bash -c \"psql \\\"\\\$DEX_DB_URL_DIRECT\\\" -tAc \\\"SELECT 1 FROM pg_tables WHERE schemaname='ops' AND tablename='doctors_clinicians_ingest_runs'\\\"\" ) | grep -q 1 \
  && ( cd '$DOPPLER_DIR' && doppler run -- bash -c \"psql \\\"\\\$DEX_DB_URL_DIRECT\\\" -tAc \\\"SELECT 1 FROM information_schema.columns WHERE table_schema='entities' AND table_name='source_doctors_clinicians' AND column_name='raw_source_row'\\\"\" ) | grep -q 1 \
  && ( cd '$DOPPLER_DIR' && doppler run -- bash -c \"psql \\\"\\\$DEX_DB_URL_DIRECT\\\" -tAc \\\"SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'entities.source_doctors_clinicians'::regclass AND i.indisprimary\\\"\" ) | tr -d '[:space:]' | grep -qx 'npi'"

run_surface s1 hq-all "$S1_VERIFY"

# --- s2: code -------------------------------------------------------------- #
# File present, parses as Python, has main entry, references runs table + metastore URL.

S2_VERIFY="test -f '$SCRIPT_PATH' \
  && python3 -c \"import ast; ast.parse(open('$SCRIPT_PATH').read())\" \
  && grep -qE '(def main|__name__ == .__main__.)' '$SCRIPT_PATH' \
  && grep -q 'ops.doctors_clinicians_ingest_runs' '$SCRIPT_PATH' \
  && grep -q 'data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6' '$SCRIPT_PATH'"

run_surface s2 hq-all "$S2_VERIFY"

# --- s3 / s4: OMITTED ------------------------------------------------------ #
# s3 config: no schedule registered. Manual-first per existing pattern in
#   trigger/src/tasks/check-pluto-version.ts and refresh-usaspending-mvs.ts
#   (cron blocks commented out). A follow-up directive wires the schedule
#   after one clean prod run.
# s4 deploy: Railway data-engine-x project scheduled-for-deletion 2026-05-05.
#   No live service. Migrations apply manually via psql per
#   apps/data-engine-x/supabase/migrations/README.md.

echo "OK"
