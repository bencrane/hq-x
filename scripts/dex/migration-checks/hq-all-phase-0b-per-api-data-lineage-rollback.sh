#!/usr/bin/env bash
# Rollback harness for hq-all Phase 0b: Per-API Data Lineage
#
# All Phase 0b surfaces are additive (new modules, new middleware, additive
# record_catalog_read calls in existing services, additive header forwarding
# in dex_client). Rollback is git revert <MERGE_SHA> on origin/main + Railway
# auto-redeploy.
#
# Usage:
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-phase-0b-per-api-data-lineage-rollback.sh --merge-sha <sha>
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-phase-0b-per-api-data-lineage-rollback.sh --merge-sha <sha> --surface s4
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-phase-0b-per-api-data-lineage-rollback.sh --merge-sha <sha> --repo hq-all

set -euo pipefail

MERGE_SHA=""
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --merge-sha) MERGE_SHA="$2"; shift 2 ;;
    --surface)   SURFACE_FILTER="$2"; shift 2 ;;
    --repo)      REPO_FILTER="$2"; shift 2 ;;
    *)           echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$MERGE_SHA" ]]; then
  echo "FAIL — --merge-sha is required" >&2
  exit 1
fi

if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "hq-all" ]]; then
  echo "rollback: repo filter $REPO_FILTER does not match hq-all — nothing to do." >&2
  exit 0
fi

# All surfaces s1-s5 are code in the same monorepo PR; one git revert undoes
# them. s6-s8 are deploy steps (no code rollback — the revert PR triggers
# fresh deploys).
if [[ -n "$SURFACE_FILTER" ]]; then
  case "$SURFACE_FILTER" in
    s1|s2|s3|s4|s5)
      echo "rollback: surface $SURFACE_FILTER — code surfaces are revert-as-a-bundle in this monorepo. Reverting full PR."
      ;;
    s6|s7|s8)
      echo "rollback: surface $SURFACE_FILTER — deploy step. Re-deploying prior Railway deployment."
      case "$SURFACE_FILTER" in
        s6) SVC="data-engine-x" ;;
        s7) SVC="hq-x" ;;
        s8) SVC="hq-command" ;;
      esac
      PRIOR_ID=$(railway deployment list --service "$SVC" --limit 2 --json | jq -r '.[1].id')
      railway redeploy --service "$SVC" --deployment-id "$PRIOR_ID"
      exit 0
      ;;
    *) echo "FAIL — unknown surface: $SURFACE_FILTER" >&2; exit 1 ;;
  esac
fi

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
echo "==> Reverting merge SHA $MERGE_SHA on $REPO_ROOT"
echo "    (executor must run: git -C $REPO_ROOT revert -m 1 $MERGE_SHA && git push)"
echo "    (Railway will auto-redeploy data-engine-x, hq-x, hq-command on merge of revert PR)"
echo ""
echo "Manual rollback steps:"
echo "  cd $REPO_ROOT"
echo "  git checkout main && git pull"
echo "  git checkout -b hotfix/revert-hq-all-phase-0b-per-api-data-lineage-\$(date +%Y-%m-%d)"
echo "  git revert -m 1 $MERGE_SHA"
echo "  git push -u origin HEAD"
echo "  gh pr create --title \"Revert: hq-all phase 0b (per-api data lineage)\" --body \"Auto-rollback per directive iteration budget\""
echo "  gh pr merge --squash --delete-branch"
echo ""
echo "After revert merges, verify each Railway deploy returns to the prior commit:"
echo "  for svc in data-engine-x hq-x hq-command; do"
echo "    railway deployment list --service \$svc --limit 1 --json | jq -r '.[0] | {svc: \"'\$svc'\"', status, sha: .meta.commitHash}'"
echo "  done"
exit 0
