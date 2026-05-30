#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-03-clay-linkedin-posts-ingest
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (4):
#   s1  migration  apps/data-engine-x/supabase/migrations/20260503190000_source_linkedin_posts.sql
#   s2  code       apps/data-engine-x/app/services/clay_linkedin_posts.py (NEW)
#   s3  code       apps/data-engine-x/modal/dex_ingest_app.py (EXISTING — additive)
#   s4  code       apps/data-engine-x/tests/test_clay_linkedin_posts.py (NEW — flat layout)
#
# Per repo policy (forward-only with `IF NOT EXISTS`): NO paired _down.sql. All four
# surfaces ship in one PR against bencrane/hq-all main, so a single `git revert
# <merge-SHA>` undoes everything in one action. Tables remain in place post-revert
# (purely additive — no FKs out, no downstream MVs depend on them yet); ship a
# follow-up DROP TABLE migration if full table removal is needed.
#
# Per Deploy targets: Railway hq-all/data-engine-x service watches bencrane/hq-all
# main, so the revert push auto-redeploys. The new Modal route at /clay-linkedin-posts
# stays registered on the deployed Modal app until the user runs `doppler run --
# modal deploy modal/dex_ingest_app.py` post-revert (Modal deploy is OUT OF SCOPE).
# Pre-revert routing matches: until the user re-deploys Modal post-revert, the live
# Modal app still exposes the new route. This is acceptable per scope — Clay won't
# point at the route until the user kicks it off, so an "extra" route on the Modal
# app is harmless.
#
# This script ECHOES the rollback commands and asks for explicit confirmation. It
# does NOT execute anything automatically — destructive/irreversible git operations
# require a human in the loop.
#
# Usage: ./2026-05-03-clay-linkedin-posts-ingest-rollback.sh \
#          [--repo bencrane/hq-all] [--surface s1..s4] [--merge-sha <sha>] [--yes]
#
# Note: rollback action is SHARED across all four surfaces. Specifying `--surface s3`
# is a no-op-with-note — granular per-surface rollback is not supported under the
# forward-only policy.

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

HQ_ALL="/Users/benjamincrane/hq-all"
APP_DIR="$HQ_ALL/apps/data-engine-x"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
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

echo "==> Rolling back clay-linkedin-posts-ingest surfaces (filter: ${REPO_FILTER:-all})"
echo
echo "Per repo forward-only policy, all rollback actions reduce to a single git"
echo "revert + push on bencrane/hq-all main. Tables remain in place post-revert"
echo "(additive-only safety case)."
echo

# REVERSE order: last surface applied → first to roll back.
# Application order is s1 → s2 → s3 → s4, so rollback order is s4 → s3 → s2 → s1.
# The git revert is performed once on s1 — earlier steps echo a note clarifying
# their rollback is subsumed.

NOOP_NOTE='
  echo "(rollback subsumed by the single shared git revert performed in s1; per the"
  echo " forward-only policy, granular per-surface rollback is not supported — all"
  echo " four surfaces ship in one PR and revert in one action. Tables remain in"
  echo " place post-revert per the additive-only safety case; ship a follow-up"
  echo " migration with DROP TABLE IF EXISTS if full removal is desired.)"
'

S1_ROLLBACK='
  if [ -z "$MERGE_SHA" ]; then
    echo "(no --merge-sha provided; cannot stage git revert)"
    echo "Once the merge SHA is known, run:"
    echo "  git -C $HQ_ALL fetch origin main"
    echo "  git -C $HQ_ALL checkout main && git -C $HQ_ALL pull --ff-only"
    echo "  git -C $HQ_ALL revert --no-edit <merge-SHA>"
    echo "  git -C $HQ_ALL push origin main"
    echo
    echo "Railway hq-all/data-engine-x watches bencrane/hq-all main, so the push"
    echo "auto-redeploys the revert. Verify with:"
    echo "  cd $APP_DIR && railway deployment list --service data-engine-x --limit 1 --json | jq -e '"'"'.[0].status == \"SUCCESS\" and .[0].meta.commitHash == \"<revert-SHA>\"'"'"'"
    echo
    echo "Modal app re-deploy is out of scope; the user runs"
    echo "  doppler run -- modal deploy modal/dex_ingest_app.py"
    echo "post-revert if they want the /clay-linkedin-posts route removed from the live"
    echo "Modal app. Until then the route stays registered (harmless — Clay is not"
    echo "pointed at it yet)."
    echo
    echo "If full table removal is desired post-revert, ship a follow-up migration:"
    echo "  DROP TABLE IF EXISTS entities.source_linkedin_posts;"
    echo "  DROP TABLE IF EXISTS ops.linkedin_posts_ingest_runs;"
    exit 0
  fi
  echo
  echo "About to run on bencrane/hq-all main:"
  echo "  git revert --no-edit $MERGE_SHA"
  echo "  git push origin main"
  echo "  (Railway auto-redeploys via main-watch; Modal redeploy is out of scope)"
  echo
  if confirm "Proceed with git revert + push?"; then
    git -C "$HQ_ALL" fetch origin main
    git -C "$HQ_ALL" checkout main
    git -C "$HQ_ALL" pull --ff-only
    git -C "$HQ_ALL" revert --no-edit "$MERGE_SHA"
    git -C "$HQ_ALL" push origin main
    echo "revert pushed; tables remain in place (additive-only safety case)."
    echo "If full table removal is desired, ship a follow-up DROP TABLE migration."
    echo "Modal app still exposes /clay-linkedin-posts until user runs:"
    echo "  doppler run -- modal deploy modal/dex_ingest_app.py"
  else
    echo "aborted by operator"
    exit 1
  fi
'

# REVERSE order: s4 → s3 → s2 → s1.
rollback_surface "s4" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s3" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s2" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s1" "bencrane/hq-all" "$S1_ROLLBACK"

echo "==> All filtered surface rollbacks ran."
