#!/usr/bin/env bash
# Verifier for migration 20260504211829_phase14_irs_bmf_promotion.sql
#
# Checks:
#   1. raw_entity_records: ~1.5M new 'irs_bmf' claims, all have ein
#   2. raw_entity_address_records: ~1.5M with address_role='primary', no other roles for irs_bmf
#   3. polymorphic FK: every irs_bmf claim's source_row_id has a matching source_irs_bmf.ein
#   4. match_gov_ein: row count grew; irs_bmf rows present
#   5. resolved_entities: irs_bmf claims resolved via gov_ein
#   6. cross-source EIN clusters: IRS BMF bridges with DOL families create new mixed clusters

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

echo "=== source_irs_bmf populated ==="
src_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.source_irs_bmf")
assert_ge "source_irs_bmf rows (>= 1.4M)" "1900000" "$src_count"

echo
echo "=== new irs_bmf claims in raw_entity_records ==="
bmf_claims=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='irs_bmf'")
assert_ge "irs_bmf claims (>= 1.4M)" "1900000" "$bmf_claims"

bmf_with_ein=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='irs_bmf' AND ein IS NOT NULL")
assert "100% of irs_bmf claims have ein" "$bmf_claims" "$bmf_with_ein"

bmf_with_name=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='irs_bmf' AND name IS NOT NULL")
assert "100% of irs_bmf claims have name" "$bmf_claims" "$bmf_with_name"

echo
echo "=== polymorphic FK to source_irs_bmf ==="
fk_orphans=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records r LEFT JOIN entities.source_irs_bmf s ON s.ein = r.source_row_id WHERE r.source_name='irs_bmf' AND s.ein IS NULL")
assert "no irs_bmf claims orphan from source_irs_bmf" "0" "$fk_orphans"

echo
echo "=== sibling backfill: irs_bmf addresses (role='primary') ==="
bmf_addrs=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='irs_bmf' AND a.address_role='primary'")
assert_ge "irs_bmf address sibling rows (>= 1.8M; some BMF rows lack address)" "1800000" "$bmf_addrs"

bmf_addr_other_roles=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='irs_bmf' AND a.address_role <> 'primary'")
assert "no irs_bmf addresses with role other than 'primary'" "0" "$bmf_addr_other_roles"

echo
echo "=== match_gov_ein: irs_bmf EINs surfacing ==="
ein_total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_ein")
assert_ge "match_gov_ein total (>= 4.5M after BMF added)" "4500000" "$ein_total"

bmf_in_match=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_ein m JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id WHERE r.source_name='irs_bmf'")
assert_ge "match_gov_ein irs_bmf rows (>= 1.4M)" "1900000" "$bmf_in_match"

echo
echo "=== resolved_entities: irs_bmf claims resolved via gov_ein ==="
bmf_resolved=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities re JOIN entities.raw_entity_records r ON r.raw_entity_id = re.raw_entity_id WHERE r.source_name='irs_bmf' AND re.resolved_via_method='gov_ein'")
assert_ge "irs_bmf claims resolved via gov_ein (>= 1.4M)" "1900000" "$bmf_resolved"

echo
echo "=== cross-source EIN clusters: IRS BMF bridges with DOL families ==="
# Heavy analytical query — use dex_psql_query_direct (statement_timeout=0).
mixed_clusters=$(dex_psql_query_direct "
  SELECT COUNT(*)::bigint
  FROM (
    SELECT m.match_value
    FROM entities.match_gov_ein m
    JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
    GROUP BY m.match_value
    HAVING COUNT(DISTINCT r.source_name) > 1
       AND BOOL_OR(r.source_name = 'irs_bmf')
       AND BOOL_OR(r.source_name LIKE 'dol_form_5500%')
  ) AS bmf_dol_clusters
")
assert_ge "IRS BMF ↔ DOL Form 5500 family mixed-source EIN clusters (>= 1k)" "1000" "$mixed_clusters"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then exit 1; fi
exit 0
