#!/usr/bin/env bash
# Rollback harness for directive: cms-doctors-clinicians-ingest-2026-05-03
# Filled by AUDIT subagent on 2026-05-03.
#
# Surfaces (post-audit, 2 active):
#   s1 migration  DROP entities.source_doctors_clinicians + ops.doctors_clinicians_ingest_runs
#                 via paired DOWN migration file (apps/data-engine-x/supabase/migrations/
#                 20260503160000_doctors_clinicians_source_tables_down.sql)
#   s2 code       git revert -m 1 <merge-SHA> && git push origin main
#
# Surfaces OMITTED (no rollback target needed):
#   s3 config     no schedule registered
#   s4 deploy     no deploy surface (Railway project deleted)
#
# Usage: ./cms-doctors-clinicians-ingest-2026-05-03-rollback.sh [--repo hq-all] [--surface s1|s2]

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
DOWN_MIGRATION="$APP_DIR/supabase/migrations/20260503160000_doctors_clinicians_source_tables_down.sql"

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

# --- s1 rollback: apply DOWN migration ------------------------------------- #
# DDL → DEX_DB_URL_DIRECT. doppler run + bash -c per the CLAUDE.md gotcha.
# The DOWN file is committed alongside the forward migration; if the executor
# wrote the migration to a different timestamp prefix, override DOWN_MIGRATION
# via the env var.

S1_ROLLBACK="test -f '$DOWN_MIGRATION' \
  && ( cd '$DOPPLER_DIR' && doppler run -- bash -c 'psql \"\$DEX_DB_URL_DIRECT\" -f \"$DOWN_MIGRATION\"' )"

run_surface s1 hq-all "$S1_ROLLBACK"

# --- s2 rollback: git revert ----------------------------------------------- #
# Operator-driven; emit the canonical command for the runbook. The harness
# does NOT auto-revert (would require knowing the merge SHA, which is not
# available pre-merge). Verification: this surface "PASS" means the command
# is documented and the operator has the SHA.

S2_ROLLBACK="echo 'Manual: git revert -m 1 <merge-SHA> && git push origin main  (or gh pr revert <pr-number>)' && echo 'Audit-confirmed canonical s2 rollback path.'"

run_surface s2 hq-all "$S2_ROLLBACK"

# --- s3 / s4: OMITTED ------------------------------------------------------ #
# s3: no schedule registered → no rollback target.
# s4: no deploy surface → no rollback target. (Railway data-engine-x project
#     scheduled-for-deletion 2026-05-05; no service to revert. Migrations are
#     manually applied so the s2 git revert + s1 DOWN apply is the full path.)

echo "OK"
