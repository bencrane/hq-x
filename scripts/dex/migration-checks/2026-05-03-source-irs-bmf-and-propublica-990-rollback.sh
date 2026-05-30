#!/usr/bin/env bash
# Rollback harness for directive: 2026-05-03-source-irs-bmf-and-propublica-990
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (4 — s5 removed per operator decision [2]):
#   s1  migration  apps/data-engine-x/supabase/migrations/20260503120000_source_irs_bmf.sql
#                  (creates entities.source_irs_bmf + ops.irs_bmf_ingest_runs)
#   s2  migration  apps/data-engine-x/supabase/migrations/20260503130000_source_propublica_nonprofits.sql
#                  (creates entities.source_propublica_nonprofits + ops.propublica_nonprofit_ingest_runs)
#   s3  code       apps/data-engine-x/scripts/run_irs_bmf_ingest.py
#   s4  code       apps/data-engine-x/scripts/run_propublica_nonprofit_ingest.py
#
# Per operator decision [5]: forward-only migrations with `IF NOT EXISTS`. NO
# paired _down.sql files. Rollback for ALL surfaces is the same single action:
# `git revert <merge-SHA>` on bencrane/hq-all main + push. Tables remain in place
# (purely additive — no FKs out, no downstream MVs depend on them yet); a follow-
# up directive can drop them via `DROP TABLE IF EXISTS` if desired.
#
# Per operator decision [2]: there is no Railway deploy step in this directive,
# so the revert push does NOT auto-redeploy. The Railway data-engine-x project
# is currently watching `bencrane/data-engine-x` (deprecated) — until that watch
# is moved to `bencrane/hq-all` in a follow-up scope, the revert is a code-and-
# DB-only rollback. No service depends on these tables yet, so prod state
# remains coherent regardless.
#
# This script ECHOES the rollback commands and asks for explicit confirmation.
# It does NOT execute anything automatically — destructive/irreversible git
# operations require a human in the loop.
#
# Usage: ./2026-05-03-source-irs-bmf-and-propublica-990-rollback.sh \
#          [--repo bencrane/hq-all] [--surface s1..s4] [--merge-sha <sha>] \
#          [--yes]
#
# Note: the rollback action is SHARED by all four surfaces (one git revert
# undoes everything). Specifying `--surface s3` is a no-op-with-note because
# a single-surface rollback isn't possible under forward-only — the only way
# to undo s3 alone post-merge would be a follow-up commit removing the script
# file, which is functionally equivalent to a per-surface revert. The harness
# documents this rather than pretending granular rollback is supported.

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

echo "==> Rolling back source_irs_bmf + source_propublica_nonprofits surfaces (filter: ${REPO_FILTER:-all})"
echo
echo "Per operator decision [5] (forward-only with IF NOT EXISTS), all rollback"
echo "actions reduce to a single git revert + push on bencrane/hq-all main."
echo "Per operator decision [2], there is no Railway deploy step to roll back."
echo

# REVERSE order per SKILL.md: last surface applied → first to roll back.
# Order of application was s1 → s2 → s3 → s4, so rollback order is s4 → s3 → s2 → s1.
# The git revert is performed once on the LAST step (s1) — earlier steps just echo
# a note clarifying that their rollback is subsumed by the single shared revert.

NOOP_NOTE='
  echo "(rollback subsumed by the single shared git revert performed in s1; per Q5=B"
  echo " forward-only policy, granular per-surface rollback is not supported — all four"
  echo " surfaces ship in one PR and revert in one action. Tables remain in place"
  echo " post-revert per the additive-only safety case; ship a follow-up migration"
  echo " with DROP TABLE IF EXISTS if full removal is desired.)"
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
    echo "Per operator decision [2], no Railway redeploy follows the push — the"
    echo "data-engine-x service still watches bencrane/data-engine-x (deprecated)"
    echo "until follow-up scope rewires it to bencrane/hq-all."
    echo
    echo "If full table removal is desired post-revert, ship a follow-up migration:"
    echo "  DROP TABLE IF EXISTS entities.source_irs_bmf;"
    echo "  DROP TABLE IF EXISTS entities.source_propublica_nonprofits;"
    echo "  DROP TABLE IF EXISTS ops.irs_bmf_ingest_runs;"
    echo "  DROP TABLE IF EXISTS ops.propublica_nonprofit_ingest_runs;"
    exit 0
  fi
  echo
  echo "About to run on bencrane/hq-all main:"
  echo "  git revert --no-edit $MERGE_SHA"
  echo "  git push origin main"
  echo "  (no Railway redeploy — see operator decision [2])"
  echo
  if confirm "Proceed with git revert + push?"; then
    git -C "$HQ_ALL" fetch origin main
    git -C "$HQ_ALL" checkout main
    git -C "$HQ_ALL" pull --ff-only
    git -C "$HQ_ALL" revert --no-edit "$MERGE_SHA"
    git -C "$HQ_ALL" push origin main
    echo "revert pushed; tables remain in place (additive-only safety case)."
    echo "If full table removal is desired, ship a follow-up DROP TABLE migration."
  else
    echo "aborted by operator"
    exit 1
  fi
'

# REVERSE order: s4 → s3 → s2 → s1 (s1 is where the actual revert runs).
rollback_surface "s4" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s3" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s2" "bencrane/hq-all" "$NOOP_NOTE"
rollback_surface "s1" "bencrane/hq-all" "$S1_ROLLBACK"

echo "==> All filtered surface rollbacks ran."
