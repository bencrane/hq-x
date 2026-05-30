#!/usr/bin/env bash
# Rollback harness for directive: cms-open-payments-ingest-2026-05-03
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
# Forward-only policy: DEX has no `_down.sql` files anywhere under
# apps/data-engine-x/supabase/migrations/. Rollback DDL lives in this script
# alone, same as the parent IRS+ProPublica sub-directive.
#
# These tables are net-new and additive — nothing reads from them at rollback
# time (no MV depends on them, no service queries them, no FKs out). Direct
# DROP CASCADE is safe and matches the directive's `## Rollback harness` plan.
#
# Reversal order: s2 (code revert) → s1 (DDL DROPs).
#
# This script ECHOES rollback commands and asks for explicit confirmation
# before executing. `--yes` skips prompts (use only in fully-automated runs).
#
# Apply-mechanism reminder: migrations are applied MANUALLY post-merge / pre-PR
# via doppler-wrapped psql -f. The s1 rollback also DELETEs the version row
# from supabase_migrations.schema_migrations so a future re-application replays
# the up-migration cleanly.
#
# Usage: ./cms-open-payments-ingest-2026-05-03-rollback.sh \
#          [--repo bencrane/hq-all] [--surface s1|s2] [--merge-sha <sha>] [--yes]

set -euo pipefail

REPO_FILTER=""
SURFACE_FILTER=""
MERGE_SHA=""
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --merge-sha) MERGE_SHA="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift 1 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

WORKTREE="${DEX_WORKTREE_DIR:-/Users/benjamincrane/hq-all/.claude/worktrees/pedantic-pascal-dd9a57}"
APP_DIR="${DEX_APP_DIR:-$WORKTREE/apps/data-engine-x}"
# Main worktree (where `main` branch lives) — used only by s2 rollback's
# git revert + push flow. `git -C $MAIN_WORKTREE` checks out main locally
# before the revert. WORKTREE above is the per-directive branch worktree.
MAIN_WORKTREE="${DEX_MAIN_WORKTREE:-/Users/benjamincrane/hq-all}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

REMOTE=$(git -C "$WORKTREE" remote get-url origin 2>&1 || echo "MISSING")
if [[ "$REMOTE" != *"bencrane/hq-all"* ]]; then
  echo "FAIL: $WORKTREE origin is '$REMOTE' — expected bencrane/hq-all" >&2
  exit 1
fi

confirm() {
  local prompt="$1"
  if [[ "$ASSUME_YES" == "1" ]]; then
    echo "(--yes) auto-confirming: $prompt"
    return 0
  fi
  read -r -p "$prompt [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

echo "==> Rolling back CMS Open Payments surfaces (filter: ${REPO_FILTER:-all})"

# ---------------------------------------------------------------------------- #
# s2 rollback (REVERSE order — last applied, first reverted): git revert the
# merge SHA on bencrane/hq-all main. The migration file is reverted as a side
# effect of the revert; the actual DB DDL is still present until s1 runs.
# ---------------------------------------------------------------------------- #
S2_ROLLBACK='
  if [ -z "$MERGE_SHA" ]; then
    echo "(no --merge-sha provided; cannot stage git revert)"
    echo "Once the merge SHA is known, run:"
    echo "  git -C $MAIN_WORKTREE fetch origin main"
    echo "  git -C $MAIN_WORKTREE checkout main && git -C $MAIN_WORKTREE pull --ff-only"
    echo "  git -C $MAIN_WORKTREE revert --no-edit <merge-SHA>"
    echo "  git -C $MAIN_WORKTREE push origin main"
    echo
    echo "(Then run this harness again with --surface s1 to drop the four tables.)"
    exit 0
  fi
  echo
  echo "About to run on bencrane/hq-all main:"
  echo "  git revert --no-edit $MERGE_SHA"
  echo "  git push origin main"
  echo
  if confirm "Proceed with git revert + push?"; then
    git -C "$MAIN_WORKTREE" fetch origin main
    git -C "$MAIN_WORKTREE" checkout main
    git -C "$MAIN_WORKTREE" pull --ff-only
    git -C "$MAIN_WORKTREE" revert --no-edit "$MERGE_SHA"
    git -C "$MAIN_WORKTREE" push origin main
    echo "revert pushed; tables remain in DB until s1 rollback runs."
  else
    echo "aborted by operator"
    exit 1
  fi
'

# ---------------------------------------------------------------------------- #
# s1 rollback: direct DROP TABLE IF EXISTS ... CASCADE for all four tables, in
# a single transaction together with DELETE FROM supabase_migrations.schema_migrations
# so the migration is fully unbookkept and can be re-applied later if needed.
# Safe because (a) tables are net-new this PR, (b) nothing else depends on them
# (no MVs, no FKs out, no services), (c) CASCADE handles any accidental
# dependencies cleanly, (d) the DELETE removes ONLY the row for our migration
# version (matched by exact ts prefix).
# ---------------------------------------------------------------------------- #
S1_ROLLBACK='
  echo
  echo "About to run on prod DEX_DB_URL_DIRECT (single transaction):"
  echo "  BEGIN;"
  echo "  DROP TABLE IF EXISTS entities.source_cms_open_payments_general   CASCADE;"
  echo "  DROP TABLE IF EXISTS entities.source_cms_open_payments_research  CASCADE;"
  echo "  DROP TABLE IF EXISTS entities.source_cms_open_payments_ownership CASCADE;"
  echo "  DROP TABLE IF EXISTS ops.cms_open_payments_ingest_runs           CASCADE;"
  echo "  DELETE FROM supabase_migrations.schema_migrations WHERE version = '"'"'20260503160000'"'"';"
  echo "  COMMIT;"
  echo
  if confirm "Proceed with DROP CASCADE on all four tables + unbookkeep migration?"; then
    cd "$APP_DIR" && \
    doppler run -p hq-all -c prd -- bash -c '"'"'
      set -e
      psql "$DEX_DB_URL_DIRECT" -v ON_ERROR_STOP=1 <<SQL
        BEGIN;
        DROP TABLE IF EXISTS entities.source_cms_open_payments_general   CASCADE;
        DROP TABLE IF EXISTS entities.source_cms_open_payments_research  CASCADE;
        DROP TABLE IF EXISTS entities.source_cms_open_payments_ownership CASCADE;
        DROP TABLE IF EXISTS ops.cms_open_payments_ingest_runs           CASCADE;
        DELETE FROM supabase_migrations.schema_migrations WHERE version = '"'"'"'"'"'"'"'"'20260503160000'"'"'"'"'"'"'"'"';
        COMMIT;
SQL
      echo "all four tables dropped + migration unbookkept"
    '"'"'
  else
    echo "aborted by operator"
    exit 1
  fi
'

# REVERSE order per skill spec: s2 (last applied) → s1 (first applied).
rollback_surface "s2" "bencrane/hq-all" "$S2_ROLLBACK"
rollback_surface "s1" "bencrane/hq-all" "$S1_ROLLBACK"

echo "==> All filtered surface rollbacks ran."
