#!/usr/bin/env bash
# Verifier for migration 20260504184838_create_dol_admin_claims.sql

set -euo pipefail

HQ_SHIM="${HOME}/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
if [[ -f "$HQ_SHIM" ]]; then
  source "$HQ_SHIM"
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo)"
  if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    source "$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
  else
    echo "FAIL: cannot locate dex.sh helpers" >&2; exit 1
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

echo "=== new admin claims in raw_entity_records ==="
admin_5500=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500_admin'")
assert_ge "dol_form_5500_admin claims (>= 30k)" "30000" "$admin_5500"

admin_sf=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500_sf_admin'")
assert_ge "dol_form_5500_sf_admin claims (>= 190k)" "190000" "$admin_sf"

echo
echo "=== admin claims have EINs ==="
admin_5500_with_ein=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500_admin' AND ein IS NOT NULL")
assert "100% of dol_form_5500_admin claims have ein" "$admin_5500" "$admin_5500_with_ein"

admin_sf_with_ein=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500_sf_admin' AND ein IS NOT NULL")
assert "100% of dol_form_5500_sf_admin claims have ein" "$admin_sf" "$admin_sf_with_ein"

echo
echo "=== sibling backfills: admin phones ==="
admin_phones=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name IN ('dol_form_5500_admin','dol_form_5500_sf_admin')")
assert_ge "admin phone records (>= 200k)" "200000" "$admin_phones"

echo
echo "=== sibling backfills: admin addresses ==="
admin_us_addrs=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name IN ('dol_form_5500_admin','dol_form_5500_sf_admin') AND a.address_role IN ('admin_us','sf_admin_us')")
assert_ge "admin US address records (>= 200k)" "200000" "$admin_us_addrs"

echo
echo "=== match_gov_ein refreshed: admin EINs surfacing ==="
ein_total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_ein")
assert_ge "match_gov_ein total (>= 2.6M after admin claims added)" "2600000" "$ein_total"

distinct_sources=$(dex_psql_query "SELECT COUNT(DISTINCT r.source_name)::int FROM entities.match_gov_ein m JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id")
assert_ge "match_gov_ein covers >=5 source_name values (sponsor3 + admin2)" "5" "$distinct_sources"

echo
echo "=== resolved_entities updated: admin claims now have canonicals ==="
admin_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities re JOIN entities.raw_entity_records r ON r.raw_entity_id = re.raw_entity_id WHERE r.source_name IN ('dol_form_5500_admin','dol_form_5500_sf_admin') AND re.resolved_via_method='gov_ein'")
assert_ge "admin claims resolved via gov_ein (>= 200k)" "200000" "$admin_resolved"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then exit 1; fi
exit 0
