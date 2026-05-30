#!/usr/bin/env bash
# Verifier for migration 20260504162122_add_gov_uei_gov_duns_match_mvs.sql

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

echo "=== schema check ==="
for mv in match_gov_uei match_gov_duns; do
  exists=$(dex_psql_query "SELECT 1 FROM pg_matviews WHERE schemaname='entities' AND matviewname='$mv'")
  assert "$mv MV exists" "1" "$exists"
  uniq_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='$mv' AND indexname='uq_${mv}_claim_value'")
  assert "$mv UNIQUE (raw_entity_id, match_value) index" "1" "$uniq_idx"
done

echo
echo "=== priority config: new rows seeded ==="
prio_count=$(dex_psql_query "SELECT COUNT(*)::int FROM entities.match_method_priority")
assert_ge "priority config has 7 methods" "7" "$prio_count"

uei_priority=$(dex_psql_query "SELECT priority FROM entities.match_method_priority WHERE method='gov_uei'")
assert "gov_uei priority = 22" "22" "$uei_priority"
duns_priority=$(dex_psql_query "SELECT priority FROM entities.match_method_priority WHERE method='gov_duns'")
assert "gov_duns priority = 24" "24" "$duns_priority"

echo
echo "=== match MV row counts (per raw_entity_records UEI/DUNS population) ==="
uei_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_uei")
assert_ge "match_gov_uei rows (>= 90k)" "90000" "$uei_rows"

duns_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_duns")
assert_ge "match_gov_duns rows (>= 100k)" "100000" "$duns_rows"

echo
echo "=== resolved_entities: refresh picked up new methods ==="
uei_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_uei'")
assert_ge "claims now resolved via gov_uei (>= 1k — many SBIR claims previously resolved via lower priority)" "1000" "$uei_resolved"

duns_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_duns'")
# DUNS-resolved claims = claims with DUNS but no LEI/EIN/UEI. Could be small if most SBIR claims have UEI.
assert_ge "claims resolved via gov_duns (>= 0; OK to be small if UEI dominates)" "0" "$duns_resolved"

echo
echo "=== invariant: priority semantics still hold ==="
violations=$(dex_psql_query "
  SELECT COUNT(*)::bigint
  FROM entities.resolved_entities re
  WHERE EXISTS (
    SELECT 1 FROM entities.match_assertions_with_canonical mawc
    WHERE mawc.raw_entity_id = re.raw_entity_id
      AND mawc.priority < re.resolved_via_priority
  )
")
assert "no claim resolved by lower-precedence method when higher exists" "0" "$violations"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then exit 1; fi
exit 0
