#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-04-source-warn-notices-nv
# Audit-filled. Reverses the migration in REVERSE order (s4 -> s3 -> s2 -> s1).
#
# s4 (NV DB rows):
#   Operator-confirmed: DELETE rows WHERE state='NV' is safe (additive ingest).
#   No-op unless STATE_CLEANUP=1 is set (rows are idempotent on re-deploy).
#
# s3 (Modal redeploy):
#   `modal app rollback data-engine-x-warn-notices-ingest` - first-class CLI
#   command that redeploys the prior version (the FL/TX/NJ image).
#   The prior image won't dispatch on --state nv args; behavior degrades
#   cleanly to "unknown state" error.
#
# s2 + s1 (code):
#   Single `git revert <merge-SHA>` covers both code surfaces (one PR ships
#   the entire NV slice). pyproject.toml pdfplumber dep also reverts with the
#   same commit.
#
# MERGE_SHA env var is required for code revert.
#
# Usage:
#   MERGE_SHA=abc1234 ./source-warn-notices-nv-rollback.sh
#   ./source-warn-notices-nv-rollback.sh --surface s2
#   STATE_CLEANUP=1 ./source-warn-notices-nv-rollback.sh --surface s4

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

echo "==> Rolling back source-warn-notices-nv surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

# REVERSE order: s4 -> s3 -> s2 -> s1.

# -------------------------------------------------------------------------- #
# s4 - DB rows: optional NV cleanup. Rows are additive — safe to leave in
# place on rollback (re-deploy will re-upsert idempotently).
# -------------------------------------------------------------------------- #
rollback_surface "s4" "data-engine-x" '\
  if [[ "${STATE_CLEANUP:-0}" == "1" ]]; then \
    echo "s4: STATE_CLEANUP=1 -> DELETE NV rows" && \
    dex_psql_ddl "DELETE FROM entities.source_warn_notices WHERE state = '"'"'NV'"'"';" && \
    dex_psql_ddl "DELETE FROM ops.warn_notices_ingest_runs WHERE state = '"'"'NV'"'"';"; \
  else \
    echo "s4: no-op (STATE_CLEANUP unset; rows are additive)"; \
    true; \
  fi'

# -------------------------------------------------------------------------- #
# s3 - Modal redeploy: roll back to prior (FL/TX/NJ-only) image.
# -------------------------------------------------------------------------- #
rollback_surface "s3" "data-engine-x" 'cd "$APP_DIR" && \
  if doppler run --project hq-all --config prd -- modal app rollback data-engine-x-warn-notices-ingest 2>&1 | tee /tmp/warn_notices_nv_rollback_s3.log; then \
    echo "s3: rolled back to previous deployed version (FL/TX/NJ image)"; \
  else \
    echo "s3: rollback failed - falling back to stop" >&2; \
    doppler run --project hq-all --config prd -- modal app stop data-engine-x-warn-notices-ingest; \
  fi'

# -------------------------------------------------------------------------- #
# s2 - subsumed by s1 git revert (single MERGE_SHA covers both code surfaces).
# -------------------------------------------------------------------------- #
rollback_surface "s2" "data-engine-x" 'echo "s2 rollback subsumed by s1 git revert (single MERGE_SHA covers both code surfaces)" && true'

# -------------------------------------------------------------------------- #
# s1 - code: open hotfix-revert PR on bencrane/hq-all and auto-merge it.
# -------------------------------------------------------------------------- #
rollback_surface "s1" "data-engine-x" 'if [[ -z "${MERGE_SHA:-}" ]]; then echo "FAIL: MERGE_SHA env var must be set for s1/s2 rollback" >&2; exit 2; fi && \
  cd "$HQ_ALL" && \
  git fetch origin main && \
  BR="hotfix/revert-source-warn-notices-nv-$(date -u +%Y%m%d-%H%M%S)" && \
  git checkout -b "$BR" origin/main && \
  git revert --no-edit "$MERGE_SHA" && \
  git push -u origin "$BR" && \
  gh -R bencrane/hq-all pr create --title "Revert: 2026-05-04-source-warn-notices-nv" --body "Auto-generated revert. Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-04-source-warn-notices-nv.md" --base main && \
  gh -R bencrane/hq-all pr merge --merge --auto --delete-branch'

echo "==> Rollback complete."
