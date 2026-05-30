#!/usr/bin/env bash
# Verification harness for hq-all-phase-5-matching-engine-scaffold.
#
# Runs ALL surface verify commands. Exits 0 iff every check passes.
# Accepts --repo <name> to filter to one repo's surfaces.
#
# Source from any cwd via the vault shim (locates the canonical _lib/dex.sh).

set -uo pipefail

# Locate the canonical hq-all checkout (operator uses ~/hq-all, not ~/Desktop/hq-all
# per memory `no_canonical_clone_concept.md`).
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done

if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (filter: ${REPO_FILTER:-all})"

# --- hq-x DB helpers (HQX_DB_URL_DIRECT, distinct from DEX) ---------------
# Doppler shell gotcha: env vars must be referenced inside bash -c '...' so
# Doppler expands them at runtime, not at compose time. See
# apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha".
_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}

hqx_psql_query() {
  local sql="$1"
  _hqx_doppler "psql \"\$HQX_DB_URL_DIRECT\" -v ON_ERROR_STOP=1 -tAc \"$sql\""
}

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# --- s1: migration applied (3 tables) -----------------------------------
run_surface "s1" "hq-all" '
  expected=3
  actual=$(hqx_psql_query "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '"'"'business'"'"' AND table_name IN ('"'"'matching_relationships'"'"', '"'"'matches'"'"', '"'"'match_surfacings'"'"')")
  test "$actual" = "$expected"
'

# --- s1-seed: seed relationship row -------------------------------------
run_surface "s1-seed" "hq-all" '
  actual=$(hqx_psql_query "SELECT COUNT(*) FROM business.matching_relationships WHERE name = '"'"'demand_side_fulfillment_paid_spec_v1'"'"'")
  test "$actual" = "1"
'

# --- s2/s3/s4/s5/s6: code package imports cleanly -------------------------
# (Single combined check — if any import fails, this catches it.)
run_surface "s2-s6" "hq-all" '
  cd "$HOME/hq-all/apps/hq-x" && doppler run --project hq-all --config dev -- uv run python -c "
from app.services.matching_engine import engine, persistence, models
from app.services.matching_engine.surfacing import portal, operator_queue, cold_email_handoff
print(\"OK\")
" 2>&1 | tail -1 | grep -q "^OK$"
'

# --- s7: router file exists and registers in main.py ----------------------
run_surface "s7" "hq-all" '
  test -f "$HOME/hq-all/apps/hq-x/app/routers/matches_v1.py" &&
  grep -q "matches_v1" "$HOME/hq-all/apps/hq-x/app/main.py"
'

# --- s8: Trigger.dev cron file exists ------------------------------------
run_surface "s8" "hq-all" '
  test -f "$HOME/hq-all/apps/hq-x/src/trigger/matching-engine-daily.ts" &&
  grep -q "schedules.task" "$HOME/hq-all/apps/hq-x/src/trigger/matching-engine-daily.ts" &&
  grep -q "0 8 \* \* \*" "$HOME/hq-all/apps/hq-x/src/trigger/matching-engine-daily.ts"
'

# --- s9: smoke test exists and runs --------------------------------------
run_surface "s9" "hq-all" '
  test -f "$HOME/hq-all/apps/hq-x/scripts/smoke_matching_engine.py" &&
  cd "$HOME/hq-all/apps/hq-x" && doppler run --project hq-all --config prd -- uv run python scripts/smoke_matching_engine.py
'

# --- s10: docs exist -----------------------------------------------------
run_surface "s10" "hq-all" '
  test -f "$HOME/hq-all/apps/hq-x/docs/matching-engine.md" &&
  test -s "$HOME/hq-all/apps/hq-x/docs/matching-engine.md"
'

# --- s11: Railway hq-x service deployed at expected SHA + SUCCESS ---------
# Skip in pre-deploy mode (no MERGE_SHA env); deploy-verifier sets MERGE_SHA.
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s11" "hq-all" '
    doppler run --project hq-all --config prd -- railway status --json |
      python3 -c "
import json, sys
data = json.load(sys.stdin)
prod = next(e[\"node\"] for e in data[\"environments\"][\"edges\"] if e[\"node\"][\"name\"]==\"production\")
hqx = next(s[\"node\"] for s in prod[\"serviceInstances\"][\"edges\"] if s[\"node\"][\"serviceName\"]==\"hq-x\")
dep = hqx.get(\"latestDeployment\") or {}
sha = (dep.get(\"meta\") or {}).get(\"commitHash\",\"\")
status = dep.get(\"status\",\"\")
print(f\"{status} {sha[:8]}\")
sys.exit(0 if status==\"SUCCESS\" and sha.startswith(\"$MERGE_SHA\"[:8]) else 1)
"
  '
  run_surface "s11-prod-url" "hq-all" '
    code=$(curl -s -o /dev/null -w "%{http_code}" https://api.opsengine.run/healthz)
    test "$code" = "200" || test "$code" = "204"
  '
  run_surface "s11-operator-queue" "hq-all" '
    code=$(curl -s -o /dev/null -w "%{http_code}" https://api.opsengine.run/api/v1/operator/match-queue)
    test "$code" = "200" || test "$code" = "401" || test "$code" = "403"
  '
else
  echo "-- s11 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
fi

# --- s12: Trigger.dev cron registered ----------------------------------
if [[ -n "${TRIGGER_DEPLOYED:-}" ]]; then
  run_surface "s12" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      curl -fsS https://api.trigger.dev/api/v1/schedules \
        -H \"Authorization: Bearer \$TRIGGER_SECRET_KEY\" |
      python3 -c \"
import json, sys
data = json.load(sys.stdin)
schedules = data.get(\\\"data\\\", data) if isinstance(data, dict) else data
for s in schedules:
    if s.get(\\\"task\\\") == \\\"matching-engine-daily\\\":
        print(\\\"FOUND\\\")
        sys.exit(0)
sys.exit(1)
\"
    "
  '
else
  echo "-- s12 (hq-all): SKIPPED (set TRIGGER_DEPLOYED=1 to verify Trigger.dev)"
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
