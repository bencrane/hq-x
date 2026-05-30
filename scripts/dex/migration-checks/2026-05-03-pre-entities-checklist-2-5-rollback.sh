#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-03-pre-entities-checklist-2-5
# Reverse-order rollback (s3 -> s2 -> s1a -> s1b). Apply order was
# s1b -> s1a -> s2 -> s3, so reverse is s3 -> s2 -> s1a -> s1b. Removing
# the hook (s1a) before emptying the ledger (s1b) is the safe order: the
# hook can't fire migration applies after it's gone, so the ledger empty
# can't trigger anything destructive.
#
# IMPORTANT: This script DOES NOT auto-revert merged PRs. For s2 and s3,
# the rollback target is `git revert <merge-SHA>` against the respective
# repo — that is an intentional human-in-the-loop step. This script verifies
# that the rollback CAN be done and prints the exact command to run.
#
# For s1a and s1b (local-only / DB-only, no PR), this script DOES execute
# the rollback when invoked with --execute. By default it is dry-run and
# only prints what it would do.
#
# Usage:
#   ./2026-05-03-pre-entities-checklist-2-5-rollback.sh             # dry-run (default)
#   ./2026-05-03-pre-entities-checklist-2-5-rollback.sh --execute   # actually run s1a/s1b rollbacks
#   ./2026-05-03-pre-entities-checklist-2-5-rollback.sh --surface s1b --execute

set -uo pipefail

export DEX_DOPPLER_PROJECT="${DEX_DOPPLER_PROJECT:-hq-all}"
export DEX_DOPPLER_CONFIG="${DEX_DOPPLER_CONFIG:-prd}"
# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

EXECUTE=0
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

VAULT="$HOME/Desktop/hq"
HQ_ALL="$HOME/hq-all"

want() {
  [[ -z "$SURFACE_FILTER" || "$SURFACE_FILTER" == "$1" ]]
}

# ---------------------------------------------------------------------------- #
# s3 rollback — git revert in ~/hq-all
# ---------------------------------------------------------------------------- #
if want s3; then
  echo "-- s3 rollback (~/hq-all dex.sh DEX_DOPPLER_PROJECT default)"
  echo "   target: git revert <s3 merge-SHA>"
  echo "   context: forward-only per supabase/migrations/README.md §\"Policy\""
  echo "            (extends to code surfaces; same pattern as recent revert PRs"
  echo "             #29, #27 in bencrane/hq-all)"
  S3_MERGE="${S3_MERGE_SHA:-}"
  if [[ -n "$S3_MERGE" ]]; then
    echo "   command: ( cd $HQ_ALL && git fetch origin main && git checkout -b revert/2026-05-03-dex-doppler-default origin/main && git revert --no-edit $S3_MERGE && git push -u origin HEAD )"
    if (( EXECUTE == 1 )); then
      echo "   NOTE: --execute set but s3 rollback is human-gated (PR creation). Skipping auto-execute."
    fi
  else
    echo "   command: set S3_MERGE_SHA=<sha> and re-run to print the exact revert command"
  fi
fi

# ---------------------------------------------------------------------------- #
# s2 rollback — git revert in vault (GitHub-backed at https://github.com/bencrane/hq.git)
# ---------------------------------------------------------------------------- #
if want s2; then
  echo "-- s2 rollback (~/Desktop/hq scope-cycle-report.sh STALE_THRESHOLD)"
  echo "   target: git revert <s2 merge-SHA>"
  echo "   context: vault is GitHub-backed (remote https://github.com/bencrane/hq.git)"
  S2_MERGE="${S2_MERGE_SHA:-}"
  if [[ -n "$S2_MERGE" ]]; then
    echo "   command: ( cd $VAULT && git fetch origin main && git checkout -b revert/2026-05-03-stale-threshold-1800 origin/main && git revert --no-edit $S2_MERGE && git push -u origin HEAD )"
    if (( EXECUTE == 1 )); then
      echo "   NOTE: --execute set but s2 rollback is human-gated (PR creation). Skipping auto-execute."
    fi
  else
    echo "   command: set S2_MERGE_SHA=<sha> and re-run to print the exact revert command"
  fi
fi

# ---------------------------------------------------------------------------- #
# s1a rollback — rm post-merge hook
# IMPORTANT: s1a is rolled back BEFORE s1b. The apply order was
# s1b (ledger) -> s1a (hook); reverse order is s1a -> s1b. This is the
# safe order: removing the hook first means even if `git pull` happens
# between rollbacks, no migration apply fires. If we removed the ledger
# first while the hook was still installed, the next `git pull` would
# see ~210 *.sql files as "pending" (incl. 2 *_down.sql) and apply them.
# ---------------------------------------------------------------------------- #
if want s1a; then
  echo "-- s1a rollback (~/hq-all post-merge hook)"
  HOOK="/Users/benjamincrane/hq-all/.git/hooks/post-merge"
  echo "   target: rm $HOOK"
  if [[ ! -f "$HOOK" ]]; then
    echo "   already absent — nothing to do"
  elif (( EXECUTE == 1 )); then
    echo "   executing..."
    rm -f "$HOOK"
    if [[ -f "$HOOK" ]]; then
      echo "   FAIL: hook still present after rm" >&2
      exit 1
    fi
    echo "   s1a rollback OK"
  fi
fi

# ---------------------------------------------------------------------------- #
# s1b rollback — DELETE bootstrap rows from ops.dex_applied_migrations
# Runs AFTER s1a so the hook is already gone before the ledger is emptied.
# ---------------------------------------------------------------------------- #
if want s1b; then
  echo "-- s1b rollback (ops.dex_applied_migrations bootstrap)"
  echo "   target: DELETE FROM ops.dex_applied_migrations WHERE filename != '20260503220000_backfill_source_canonical_provenance.sql'"
  echo "   restores ledger to single-row state from before s1b"
  if (( EXECUTE == 1 )); then
    echo "   executing..."
    BEFORE=$(dex_psql_query "SELECT COUNT(*) FROM ops.dex_applied_migrations" | tr -d " ")
    dex_psql_ddl "DELETE FROM ops.dex_applied_migrations WHERE filename != '20260503220000_backfill_source_canonical_provenance.sql'" >/dev/null
    AFTER=$(dex_psql_query "SELECT COUNT(*) FROM ops.dex_applied_migrations" | tr -d " ")
    echo "   ledger row count: $BEFORE -> $AFTER"
    if [[ "$AFTER" != "1" ]]; then
      echo "   FAIL: expected 1 row remaining, got $AFTER" >&2
      exit 1
    fi
    echo "   s1b rollback OK"
  fi
fi

echo ""
if (( EXECUTE == 0 )); then
  echo "==> Dry-run complete. Re-run with --execute to apply s1a/s1b rollbacks."
  echo "    s2 and s3 rollbacks are human-gated (PR creation) and cannot be auto-executed."
else
  echo "==> Rollback execution complete (s1a/s1b only; s2/s3 require manual git revert + PR)."
fi
