#!/usr/bin/env bash
# Rollback harness for directive 2026-05-02-fdic-bank-mv-build.md
# Reverses surfaces in REVERSE order: s4 -> s3 -> s2 -> s1.
set -euo pipefail

SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

WORKTREE="/Users/benjamincrane/hq-all/.claude/worktrees/admiring-chatterjee-09ad2a"
DEX_DIR="$WORKTREE/apps/data-engine-x"
MIGRATION_FILE="$DEX_DIR/supabase/migrations/20260503130000_mv_fdic_bank_targeting_and_signal_delta_failures.sql"
DOWN_SQL="/Users/benjamincrane/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/fdic-bank-mv-build-down.sql"

rollback_surface() {
  local id="$1" repo="$2"
  shift 2
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if "$@"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2; return 1
  fi
}

# ---- surface implementations (REVERSE order) ----

# s4: revert merged PR commit if merged. Caller passes commit SHA via DEX_FDIC_MV_MERGE_SHA.
#     If unset, assume PR not merged -- no revert needed.
rollback_s4() {
  if [[ -z "${DEX_FDIC_MV_MERGE_SHA:-}" ]]; then
    echo "  DEX_FDIC_MV_MERGE_SHA unset -- assume PR not merged, no revert needed"
    return 0
  fi
  cd "$WORKTREE"
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
  git revert --no-edit "$DEX_FDIC_MV_MERGE_SHA"
  git push origin main
}

# s3: smoke counts surface — folded into s2 since dropping the MVs removes the rows.
rollback_s3() {
  echo "  s3 rollback folded into s2 (down SQL drops MVs)"
}

# s2: apply the paired down SQL via Doppler psql.
rollback_s2() {
  cd "$DEX_DIR"
  doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -f \"$DOWN_SQL\""
}

# s1: remove the migration file from the worktree if uncommitted.
#     If tracked in git, leave to s4 git revert.
rollback_s1() {
  if [[ ! -f "$MIGRATION_FILE" ]]; then
    echo "  $MIGRATION_FILE absent -- nothing to remove"
    return 0
  fi
  cd "$WORKTREE"
  if git ls-files --error-unmatch "$MIGRATION_FILE" >/dev/null 2>&1; then
    echo "  $MIGRATION_FILE tracked in git -- leave to s4 git revert"
  else
    rm -f "$MIGRATION_FILE" && echo "  removed uncommitted $MIGRATION_FILE"
  fi
}

# ---- dispatch (REVERSE order) ----

rollback_surface "s4" "hq-all" rollback_s4
rollback_surface "s3" "hq-all" rollback_s3
rollback_surface "s2" "hq-all" rollback_s2
rollback_surface "s1" "hq-all" rollback_s1

echo "Rollback complete."
