#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-03-cms-mup-phy-ingest
#
# Rollback order (REVERSE of forward order):
#   s3  → no direct rollback action; redeploy follows from s2 git revert
#   s2b → git revert <merge-SHA>  (covered by s2a's revert; same commit)
#   s2a → git revert <merge-SHA>  (single revert reverts both s2a + s2b code)
#   s1b → DROP TABLE IF EXISTS entities.source_cms_mup_phy_by_provider_and_service CASCADE;
#         DROP TABLE IF EXISTS ops.cms_mup_phy_by_provider_and_service_ingest_runs CASCADE;
#   s1a → DROP TABLE IF EXISTS entities.source_cms_mup_phy_by_provider CASCADE;
#         DROP TABLE IF EXISTS ops.cms_mup_phy_by_provider_ingest_runs CASCADE;
#
# CASCADE handles incidentally-created indexes/constraints.
# CMS MUP PHY data is public-domain CMS data with no destructive side effects on drop.
#
# Usage:
#   ./cms-mup-phy-ingest-rollback.sh [--repo data-engine-x] [--surface s1a|s1b|s2a|s2b|s3]

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

APP_DIR="${DEX_APP_DIR:-/Users/benjamincrane/hq-all/apps/data-engine-x}"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  # cd into APP_DIR so `doppler run` resolves to project=hq-all/config=prd via
  # apps/data-engine-x/doppler.yaml. Without this, running from a worktree
  # resolves to a different doppler project and DEX_DB_URL_DIRECT is empty.
  if ( cd "$APP_DIR" && eval "$cmd" ); then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# REVERSE order — last surface rolled back first.

# --- s3 rollback: no direct action -----------------------------------------
# Trigger.dev re-deploys automatically once s2's revert PR merges (if the
# revert touches trigger/**). The validator confirmed the deploy workflow
# auto-fires on merge to main with paths: trigger/** filter. Secondary CLI
# fallback (only if the auto-redeploy fails):
#
#   doppler run -- bash -c 'npx trigger.dev@latest promote <prior-version> --env prod -p proj_gunoekmmafoeqygflcmm'
#
# (See data-engine-x CLAUDE.md "Doppler Shell Gotcha".)
rollback_surface "s3" "data-engine-x" \
  'echo "  (s3) no direct rollback — Trigger.dev re-deploys on s2 revert merge; promote fallback documented in script comment"'

# --- s2b rollback: git revert (covered by s2a) -----------------------------
# Both s2a and s2b ship in the same PR / merge SHA. A single git revert handles
# both. This step is documentational; the actual revert is done once via s2a's
# block.
rollback_surface "s2b" "data-engine-x" \
  'echo "  (s2b) covered by s2a git revert — single merge SHA contains both code surfaces"'

# --- s2a rollback: git revert ----------------------------------------------
# Operator runs the revert PR via the deploy-verifier or by hand. The MERGE_SHA
# env var must be supplied at rollback time. This script validates the variable
# is set; the actual revert + push is performed by the operator (mirrors how
# every other migration in this directory documents rollback).
rollback_surface "s2a" "data-engine-x" \
  'if [[ -z "${MERGE_SHA:-}" ]]; then
    echo "  (s2a) MERGE_SHA env var not set; rollback steps:"
    echo "    cd /Users/benjamincrane/hq-all"
    echo "    git fetch && git checkout main && git pull --ff-only"
    echo "    git checkout -b hotfix/revert-cms-mup-phy-ingest-$(date +%Y%m%d%H%M%S)"
    echo "    git revert --no-edit <merge-SHA>   # supply merge SHA from PR"
    echo "    git push -u origin HEAD"
    echo "    gh -R bencrane/hq-all pr create --title \"Revert: cms-mup-phy-ingest\" --body \"Auto-revert of cms-mup-phy-ingest. See directive 2026-05-03-cms-mup-phy-ingest.md.\" --base main"
    echo "    gh pr merge --merge --auto --delete-branch"
    exit 0
  fi
  cd /Users/benjamincrane/hq-all && \
  git fetch && git checkout main && git pull --ff-only && \
  git checkout -b "hotfix/revert-cms-mup-phy-ingest-$(date +%Y%m%d%H%M%S)" && \
  git revert --no-edit "$MERGE_SHA" && \
  git push -u origin HEAD && \
  gh -R bencrane/hq-all pr create --title "Revert: cms-mup-phy-ingest" --body "Auto-revert of cms-mup-phy-ingest commit $MERGE_SHA. See directive 2026-05-03-cms-mup-phy-ingest.md." --base main && \
  gh pr merge --merge --auto --delete-branch'

# --- s1b rollback: drop by-Provider-and-Service tables ---------------------
rollback_surface "s1b" "data-engine-x" \
  'doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -c "DROP TABLE IF EXISTS entities.source_cms_mup_phy_by_provider_and_service CASCADE; DROP TABLE IF EXISTS ops.cms_mup_phy_by_provider_and_service_ingest_runs CASCADE;"'"'"

# --- s1a rollback: drop by-Provider tables ---------------------------------
rollback_surface "s1a" "data-engine-x" \
  'doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -c "DROP TABLE IF EXISTS entities.source_cms_mup_phy_by_provider CASCADE; DROP TABLE IF EXISTS ops.cms_mup_phy_by_provider_ingest_runs CASCADE;"'"'"

echo "Rollback complete."
