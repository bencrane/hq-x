#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-04-source-warn-notices-tx-nj
# Filled in by audit subagent.
#
# Reverses the migration in REVERSE order (s6 -> s5 -> s4 -> s3 -> s2 -> s1).
#
# s6 (DB rows for TX/NJ/FL re-backfill):
#   Operator-confirmed: DELETE rows WHERE state IN ('TX','NJ') if state cleanup
#   is desired. FL rows ingested by the post-deploy re-backfill are intentional
#   - this slice unblocks FL's 403 partial state - so we do NOT delete FL.
#   This step is a no-op unless STATE_CLEANUP=1 is set.
#
# s5 (Modal redeploy):
#   `modal app rollback data-engine-x-warn-notices-ingest` - first-class CLI
#   command that redeploys the previous version (the FL-only image, which is
#   harmless since the previous image won't dispatch on tx/nj args anyway).
#   Falls back to `modal app stop` if no prior version exists.
#
# s4 + s3 + s2 + s1 (code):
#   Single `git revert <merge-SHA>` covers all four code surfaces (one PR ships
#   them all). `apps/data-engine-x/pyproject.toml` openpyxl dep also reverts
#   with the same commit (already pinned at >=3.1.5 pre-existing - that's
#   actually the dep already in pyproject; s3 verifies presence, but the
#   directive's "add openpyxl" is a no-op given prior pins).
#
# MERGE_SHA env var is required for code revert. SECRET_PRE_EXISTED is N/A for
# this slice (warn-notices-db pre-existed from FL slice; do NOT delete it).
#
# Usage:
#   MERGE_SHA=abc1234 ./source-warn-notices-tx-nj-rollback.sh
#   ./source-warn-notices-tx-nj-rollback.sh --surface s2
#   ./source-warn-notices-tx-nj-rollback.sh --repo data-engine-x
#   STATE_CLEANUP=1 ./source-warn-notices-tx-nj-rollback.sh --surface s6

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

HQ_ALL="${HQ_ALL:-/Users/benjamincrane/hq-all}"
APP_DIR="$HQ_ALL/apps/data-engine-x"

# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

echo "==> Rolling back source-warn-notices-tx-nj surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

# REVERSE order: s6 -> s5 -> s4 -> s3 -> s2 -> s1.

# -------------------------------------------------------------------------- #
# s6 - DB rows: optional TX/NJ cleanup. FL rows are intentionally preserved
# (re-backfill that unblocked the 403 partial state is the slice's value-add).
# -------------------------------------------------------------------------- #
rollback_surface "s6" "data-engine-x" '\
  if [[ "${STATE_CLEANUP:-0}" == "1" ]]; then \
    echo "s6: STATE_CLEANUP=1 -> DELETE TX/NJ rows" && \
    dex_psql_ddl "DELETE FROM entities.source_warn_notices WHERE state IN ('"'"'TX'"'"','"'"'NJ'"'"');" && \
    dex_psql_ddl "DELETE FROM ops.warn_notices_ingest_runs WHERE state IN ('"'"'TX'"'"','"'"'NJ'"'"');"; \
  else \
    echo "s6: no-op (STATE_CLEANUP unset; rows are additive - re-deploy + re-backfill is idempotent)"; \
    true; \
  fi'

# -------------------------------------------------------------------------- #
# s5 - Modal redeploy: roll back to prior (FL-only) image.
# -------------------------------------------------------------------------- #
rollback_surface "s5" "data-engine-x" 'cd "$APP_DIR" && \
  if doppler run --project hq-all --config prd -- modal app rollback data-engine-x-warn-notices-ingest 2>&1 | tee /tmp/warn_notices_tx_nj_rollback_s5.log; then \
    echo "s5: rolled back to previous deployed version (FL-only image)"; \
  else \
    echo "s5: rollback failed - falling back to stop" >&2; \
    doppler run --project hq-all --config prd -- modal app stop data-engine-x-warn-notices-ingest; \
  fi'

# -------------------------------------------------------------------------- #
# s4 + s3 + s1 - subsumed by s2 git revert (single MERGE_SHA covers all four
# code surfaces in one PR).
# -------------------------------------------------------------------------- #
rollback_surface "s4" "data-engine-x" 'echo "s4 rollback subsumed by s2 git revert (single MERGE_SHA covers all four code surfaces)" && true'
rollback_surface "s3" "data-engine-x" 'echo "s3 rollback subsumed by s2 git revert (single MERGE_SHA covers all four code surfaces; pyproject.toml change reverts with same commit)" && true'

# -------------------------------------------------------------------------- #
# s2 - code: open hotfix-revert PR on bencrane/hq-all and auto-merge it.
# -------------------------------------------------------------------------- #
rollback_surface "s2" "data-engine-x" 'if [[ -z "${MERGE_SHA:-}" ]]; then echo "FAIL: MERGE_SHA env var must be set for s2/s3/s4/s1 rollback" >&2; exit 2; fi && \
  cd "$HQ_ALL" && \
  git fetch origin main && \
  BR="hotfix/revert-source-warn-notices-tx-nj-$(date -u +%Y%m%d-%H%M%S)" && \
  git checkout -b "$BR" origin/main && \
  git revert --no-edit "$MERGE_SHA" && \
  git push -u origin "$BR" && \
  gh -R bencrane/hq-all pr create --title "Revert: 2026-05-04-source-warn-notices-tx-nj" --body "Auto-generated revert. Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-04-source-warn-notices-tx-nj.md" --base main && \
  gh -R bencrane/hq-all pr merge --merge --auto --delete-branch'

# -------------------------------------------------------------------------- #
# s1 - shared helper hardening: subsumed by s2 git revert (lives in same file).
# -------------------------------------------------------------------------- #
rollback_surface "s1" "data-engine-x" 'echo "s1 rollback subsumed by s2 git revert (helper hardening lives in same file run_warn_notices_ingest.py)" && true'

echo "==> Rollback complete."
