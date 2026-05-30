#!/usr/bin/env bash
# Rollback harness for /scope directive 2026-05-04-pdl-canonical-phase-14c.
#
# Runs surface rollbacks in REVERSE order (s12 → s1). All migration / code
# surfaces (s1-s4, s7-s9, s12) roll back via `git revert <merge-SHA>` after
# merge — forward-only per apps/data-engine-x/supabase/migrations/README.md
# §"Policy". The IF NOT EXISTS / ON CONFLICT DO NOTHING idempotency means
# re-application after revert is safe.
#
# Pre-merge rollback is implicit: don't merge.
#
# MV refresh rollbacks (s5, s6) are no-ops at the data layer — a re-REFRESH
# is idempotent and restores the post-PDL state. Reverting the migration's
# .sql file just removes the timestamp tracking row; the MV state is
# preserved.
#
# Deploy rollback (s10) uses `railway redeploy --deployment-id <prior-id>`
# per apps/data-engine-x/CLAUDE.md §"Deploy targets".
#
# Endpoint rollback (s11) is implicit in s7's revert + s10's redeploy.
#
# Required env:
#   MERGE_SHA  — the merge commit SHA on main to revert (for s1-s4, s7-s9, s12)
#
# Optional env:
#   PRIOR_DEPLOY_ID — Railway deployment-id to redeploy for s10 rollback;
#                     auto-resolved via `railway deployment list` if unset.
#
# Usage:
#   MERGE_SHA=abc1234 ./2026-05-04-pdl-canonical-phase-14c-rollback.sh
#   MERGE_SHA=abc1234 ./2026-05-04-pdl-canonical-phase-14c-rollback.sh --surface s10
#   ./2026-05-04-pdl-canonical-phase-14c-rollback.sh --repo bencrane/hq-all

set -euo pipefail

HQ_ALL="${HOME}/Desktop/hq-all"

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

# A single git-revert covers s1-s4, s7-s9, s12 because they all land in the
# same PR / merge SHA. The MERGE_SHA env is gated only when a git-revert
# surface is in scope.
require_merge_sha() {
  if [[ -z "${MERGE_SHA:-}" ]]; then
    echo "FAIL: MERGE_SHA env is required for git-revert rollback (set MERGE_SHA=<merge-commit-sha>)" >&2
    return 1
  fi
}

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

# REVERSE order — last surface rolled back first.

# s12 — ENTITIES.md updates revert via the same merge SHA.
rollback_surface "s12" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s12 docs along with the rest of the PR.";
  echo "    Run once for the whole PR (see s1)."
'

# s11 — endpoint rollback piggybacks on s7 + s10. No-op here.
rollback_surface "s11" "bencrane/hq-all" '
  echo "    no-op: endpoint rollback is delegated to s7 (code revert) + s10 (railway redeploy)."
'

# s10 — Railway redeploy to prior deployment-id.
rollback_surface "s10" "bencrane/hq-all" '
  prior_id="${PRIOR_DEPLOY_ID:-}";
  if [[ -z "$prior_id" ]]; then
    prior_id=$(railway deployment list --service data-engine-x --limit 2 --json 2>/dev/null | jq -r ".[1].id");
  fi;
  [[ -n "$prior_id" && "$prior_id" != "null" ]] || { echo "FAIL: cannot resolve prior deployment id"; exit 1; };
  echo "    redeploying data-engine-x to deployment $prior_id";
  railway redeploy --service data-engine-x --deployment-id "$prior_id"
'

# s9 — test revert. Same merge SHA as the rest.
rollback_surface "s9" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s9 test removal (see s1)."
'

# s8 — MCP docstring revert. Same merge SHA.
rollback_surface "s8" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s8 docstring revert (see s1)."
'

# s7 — code revert. Same merge SHA. Combined with s10 it restores prod.
rollback_surface "s7" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s7 code revert (see s1).";
  echo "    After merging the revert, also redeploy via s10 rollback.";
'

# s6 — REFRESH resolved_entities. Idempotent; re-REFRESH is the rollback.
rollback_surface "s6" "bencrane/hq-all" '
  echo "    no-op at the data layer: REFRESH MATERIALIZED VIEW is idempotent.";
  echo "    Re-REFRESH after revert restores expected post-revert state.";
  echo "    The git-revert removes the migration tracking row (see s1)."
'

# s5 — REFRESH match_domain_etld_plus_one. Idempotent.
rollback_surface "s5" "bencrane/hq-all" '
  echo "    no-op at the data layer: REFRESH MATERIALIZED VIEW is idempotent.";
  echo "    Re-REFRESH after revert restores expected post-revert state.";
  echo "    The git-revert removes the migration tracking row (see s1)."
'

# s4 — INSERT pdl linkedin → raw_entity_linkedin_records. Reverts via merge SHA.
rollback_surface "s4" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s4 (see s1).";
  echo "    NOTE: revert removes the s4 INSERT migration file but does NOT auto-DELETE rows.";
  echo "    To clean rows, manually run: DELETE FROM entities.raw_entity_linkedin_records WHERE linkedin_role=\$\$linkedin_url\$\$ AND raw_entity_id IN (SELECT raw_entity_id FROM entities.raw_entity_records WHERE source_name=\$\$pdl_companies\$\$);"
'

# s3 — INSERT pdl websites → raw_entity_website_records. Reverts via merge SHA.
rollback_surface "s3" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s3 (see s1).";
  echo "    NOTE: revert removes the s3 INSERT migration file but does NOT auto-DELETE rows.";
  echo "    To clean rows, manually run: DELETE FROM entities.raw_entity_website_records WHERE website_role=\$\$website\$\$ AND raw_entity_id IN (SELECT raw_entity_id FROM entities.raw_entity_records WHERE source_name=\$\$pdl_companies\$\$);"
'

# s2 — INSERT pdl_companies → raw_entity_records. Reverts via merge SHA.
rollback_surface "s2" "bencrane/hq-all" '
  require_merge_sha;
  echo "    git revert $MERGE_SHA covers s2 (see s1).";
  echo "    NOTE: revert removes the s2 INSERT migration file but does NOT auto-DELETE rows.";
  echo "    s3/s4 rows reference these via FK ON DELETE CASCADE — clean s3/s4 first or delete in CASCADE order.";
  echo "    To clean rows, manually run: DELETE FROM entities.raw_entity_records WHERE source_name=\$\$pdl_companies\$\$;"
'

# s1 — CREATE TABLE raw_entity_linkedin_records. Single git revert covers
# the whole PR (s1-s4, s7-s9, s12); IF NOT EXISTS makes re-application idempotent.
rollback_surface "s1" "bencrane/hq-all" '
  require_merge_sha;
  echo "    Forward-only migrations: a SINGLE git revert of the merge SHA reverts the whole PR.";
  echo "    Run from the hq-all worktree root:";
  echo "      git -C '"$HQ_ALL"' fetch origin main";
  echo "      git -C '"$HQ_ALL"' checkout main && git -C '"$HQ_ALL"' pull";
  echo "      git -C '"$HQ_ALL"' revert --no-edit $MERGE_SHA";
  echo "      git -C '"$HQ_ALL"' push origin main";
  echo "    The revert PR removes the migration files from supabase/migrations/.";
  echo "    To DROP the new table after revert (if desired): doppler run -- bash -c \"psql \\\$DEX_DB_URL_DIRECT -c \\\"DROP TABLE IF EXISTS entities.raw_entity_linkedin_records CASCADE;\\\"\""
'

echo "Rollback complete."
