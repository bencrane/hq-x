#!/usr/bin/env bash
# Rollback harness for hq-all-phase-5-matching-engine-scaffold.
#
# All rollbacks for code/migration surfaces are `git revert <merge-SHA>`
# (per DEX migrations README forward-only policy + the same shape applied to
# code surfaces). Deploy rollbacks use the Railway / Trigger.dev platform commands.
# Accepts --surface <id> or --repo <name>.

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

# REVERSE order — deploys first (most likely to need rollback), then code, then schema.

# --- s12: Trigger.dev — redeploy prior commit ---------------------------
rollback_surface "s12" "hq-all" '
  echo "manual: cd apps/hq-x && doppler run --project hq-all --config prd -- npx trigger.dev@latest deploy from prior commit"
'

# --- s11: Railway hq-x — redeploy prior deployment --------------------
rollback_surface "s11" "hq-all" '
  prior=$(doppler run --project hq-all --config prd -- railway status --json | \
    python3 -c "
import json, sys
data = json.load(sys.stdin)
prod = next(e[\"node\"] for e in data[\"environments\"][\"edges\"] if e[\"node\"][\"name\"]==\"production\")
hqx = next(s[\"node\"] for s in prod[\"serviceInstances\"][\"edges\"] if s[\"node\"][\"serviceName\"]==\"hq-x\")
deps = hqx.get(\"deployments\") or []
# fallback: railway status doesnt return list; deploy-verifier will pre-compute
print(\"\")
")
  echo "manual: doppler run -- railway redeploy --service hq-x --yes (latest prior) — exact deployment-id lookup done by deploy-verifier"
'

# --- s2-s10: code + docs — git revert --------------------------------
rollback_surface "s2-s10" "hq-all" '
  echo "manual: git -C $HOME/hq-all revert <merge-SHA> after merge"
'

# --- s1: migration — git revert (forward-only; IF NOT EXISTS makes re-apply safe) -
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HOME/hq-all revert <merge-SHA>. Migration file uses IF NOT EXISTS so re-application is idempotent."
'

echo "Rollback complete (manual steps printed above)."
