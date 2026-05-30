#!/usr/bin/env bash
# Verifier for migration 20260504172438_add_dot_crd_columns_and_match_mvs.sql

set -euo pipefail

HQ_SHIM="${HOME}/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
if [[ -f "$HQ_SHIM" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_SHIM"
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo)"
  if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
  else
    echo "FAIL: cannot locate dex.sh helpers" >&2
    exit 1
  fi
fi

PASS_COUNT=0
FAIL_COUNT=0

assert() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "PASS: $label ($actual)"; PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "FAIL: $label — expected '$expected', got '$actual'" >&2; FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

assert_ge() {
  local label="$1" floor="$2" actual="$3"
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: $label ($actual >= $floor)"; PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "FAIL: $label — '$actual' < floor $floor" >&2; FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

echo "=== schema check: typed cols added ==="
dot_col=$(dex_psql_query "SELECT 1 FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records' AND column_name='dot_number'")
assert "raw_entity_records.dot_number column exists" "1" "$dot_col"

crd_col=$(dex_psql_query "SELECT 1 FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records' AND column_name='crd_number'")
assert "raw_entity_records.crd_number column exists" "1" "$crd_col"

echo
echo "=== backfill row counts ==="
dot_populated=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE dot_number IS NOT NULL")
assert_ge "FMCSA rows with dot_number populated (>= 6M)" "6000000" "$dot_populated"

crd_populated=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE crd_number IS NOT NULL")
assert_ge "FINRA rows with crd_number populated (>= 80k)" "80000" "$crd_populated"

echo
echo "=== match MV row counts ==="
dot_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_dot")
assert_ge "match_gov_dot rows (>= 6M)" "6000000" "$dot_rows"

crd_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_crd")
assert_ge "match_gov_crd rows (>= 80k)" "80000" "$crd_rows"

echo
echo "=== priority config: 9 methods total ==="
prio_count=$(dex_psql_query "SELECT COUNT(*)::int FROM entities.match_method_priority")
assert_ge "priority config has 9 methods" "9" "$prio_count"

dot_prio=$(dex_psql_query "SELECT priority FROM entities.match_method_priority WHERE method='gov_dot'")
assert "gov_dot priority = 26" "26" "$dot_prio"

crd_prio=$(dex_psql_query "SELECT priority FROM entities.match_method_priority WHERE method='gov_crd'")
assert "gov_crd priority = 28" "28" "$crd_prio"

echo
echo "=== resolved_entities: refresh picked up new methods ==="
dot_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_dot'")
# All 6.5M FMCSA claims should now resolve via gov_dot (highest-priority that fires
# on them; FMCSA has no LEI/EIN/UEI/DUNS).
assert_ge "claims resolved via gov_dot (>= 6M — every FMCSA claim with dot_number)" "6000000" "$dot_resolved"

crd_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_crd'")
assert_ge "claims resolved via gov_crd (>= 80k — every FINRA claim with crd_number)" "80000" "$crd_resolved"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then exit 1; fi
exit 0
