#!/usr/bin/env bash
# Verifier for migration 20260504044315_phase3b_broad_backfill_4_sources.sql
#
# Phase 3b: validates broad backfill from 4 additional sources (FMCSA,
# DOL Sch C providers, SBIR, FINRA) and the refreshed match_gov_ein MV.

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
    echo "PASS: $label ($actual)"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "FAIL: $label — expected '$expected', got '$actual'" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

assert_ge() {
  local label="$1" floor="$2" actual="$3"
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: $label ($actual >= $floor)"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "FAIL: $label — '$actual' < floor $floor" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

# Per-source: assert promoted COUNT == source COUNT (1:1 mirror integrity).
verify_source() {
  local source_name="$1" source_table="$2" expected_floor="$3"

  echo
  echo "=== $source_name: 1:1 mirror integrity + volume floor ==="

  local src_count promoted_count
  src_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.$source_table")
  promoted_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='$source_name'")
  assert "1:1 mirror: source rows = promoted rows" "$src_count" "$promoted_count"
  assert_ge "$source_name promoted volume floor" "$expected_floor" "$promoted_count"
}

verify_source "fmcsa_motor_carriers"           "motor_carrier_census_records"        6000000
verify_source "dol_form_5500_sch_c_providers"  "source_dol_form_5500_sch_c_providers" 450000
verify_source "sbir_awards"                    "source_sbir_awards"                  100000
verify_source "finra_brokercheck_firms"        "source_finra_brokercheck_firms"       80000

echo
echo "=== raw_entity_records: total volume after Phase 3b ==="

# After Phase 3b: gleif (3.3M) + 5500 (447K) + 5500_sf (1.58M)
#                 + fmcsa (6.5M) + sch_c (504K) + sbir (117K) + finra (86K)
#                 ≈ 12.5M
total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records")
assert_ge "raw_entity_records total (>= 12M)" "12000000" "$total"

distinct_sources=$(dex_psql_query "SELECT COUNT(DISTINCT source_name)::int FROM entities.raw_entity_records")
assert_ge "distinct source_name values (>= 7)" "7" "$distinct_sources"

echo
echo "=== match_gov_ein: refreshed with Sch C provider EINs ==="

# Sch C providers add another EIN-bearing source. After REFRESH, match_gov_ein
# should now cover 3 source_name values for the EIN cluster (dol_form_5500,
# dol_form_5500_sf, dol_form_5500_sch_c_providers).
distinct_ein_sources=$(dex_psql_query "
  SELECT COUNT(DISTINCT r.source_name)::int
  FROM entities.match_gov_ein m
  JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
")
assert_ge "match_gov_ein distinct source coverage (>= 3 sources after Sch C)" "3" "$distinct_ein_sources"

ein_total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_ein")
assert_ge "match_gov_ein total rows (>= 2M)" "2000000" "$ein_total"

echo
echo "=== orphan check: every promoted row resolves back to its source_* row ==="

for pair in \
  "fmcsa_motor_carriers|motor_carrier_census_records|id|=|raw_entity_records.source_row_id::uuid" \
  "finra_brokercheck_firms|source_finra_brokercheck_firms|crd_number|=|raw_entity_records.source_row_id::int"; do
  IFS='|' read -r src_name tbl src_pk op cmp <<<"$pair"
  # We don't try to type-cast for SBIR (composite key) or DOL Sch C (composite)
  # since LEFT JOIN against the typed natural key is doable but verbose; rely
  # on the row-count parity check above for those cases.

  # Cast appropriately based on PK type.
  if [[ "$src_pk" == "id" ]]; then
    orphans=$(dex_psql_query "
      SELECT COUNT(*)::bigint FROM entities.raw_entity_records r
      LEFT JOIN entities.$tbl s ON s.id::text = r.source_row_id
      WHERE r.source_name = '$src_name' AND s.id IS NULL
    ")
  else
    orphans=$(dex_psql_query "
      SELECT COUNT(*)::bigint FROM entities.raw_entity_records r
      LEFT JOIN entities.$tbl s ON s.${src_pk}::text = r.source_row_id
      WHERE r.source_name = '$src_name' AND s.${src_pk} IS NULL
    ")
  fi
  assert "$src_name promoted rows with no matching source row" "0" "$orphans"
done

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
