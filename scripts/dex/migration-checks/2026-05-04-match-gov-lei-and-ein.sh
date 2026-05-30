#!/usr/bin/env bash
# Verifier for migration 20260504042222_create_match_gov_lei_and_ein.sql
#
# Phase 2 of the entities-table initiative: validates the per-method match
# MVs are created, indexed for CONCURRENTLY refresh, and accurately reflect
# the underlying claims in entities.raw_entity_records.
#
# Usage: bash 2026-05-04-match-gov-lei-and-ein.sh

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

verify_match_mv() {
  local mv_name="$1" gov_col="$2" expected_method="$3"

  echo
  echo "=== $mv_name: schema + index check ==="

  local exists
  exists=$(dex_psql_query "SELECT 1 FROM pg_matviews WHERE schemaname='entities' AND matviewname='$mv_name'")
  assert "MV entities.$mv_name exists" "1" "$exists"

  local unique_idx
  unique_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='$mv_name' AND indexname='uq_${mv_name}_claim_value'")
  assert "UNIQUE (raw_entity_id, match_value) index exists (supports CONCURRENTLY refresh)" "1" "$unique_idx"

  local value_idx
  value_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='$mv_name' AND indexname='idx_${mv_name}_value'")
  assert "match_value index exists (cluster lookups)" "1" "$value_idx"

  echo
  echo "=== $mv_name: row count parity with raw_entity_records ==="

  local raw_count mv_count
  raw_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE $gov_col IS NOT NULL")
  mv_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.$mv_name")
  assert "MV row count = raw_entity_records WHERE $gov_col IS NOT NULL" "$raw_count" "$mv_count"

  echo
  echo "=== $mv_name: match_method literal correctness ==="

  local distinct_methods
  distinct_methods=$(dex_psql_query "SELECT COUNT(DISTINCT match_method)::int FROM entities.$mv_name")
  assert "distinct match_method count = 1" "1" "$distinct_methods"

  local method_value
  method_value=$(dex_psql_query "SELECT match_method FROM entities.$mv_name LIMIT 1")
  assert "match_method = '$expected_method'" "$expected_method" "$method_value"

  echo
  echo "=== $mv_name: no NULL match_value (every row has a real $gov_col) ==="

  local null_values
  null_values=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.$mv_name WHERE match_value IS NULL")
  assert "rows with NULL match_value" "0" "$null_values"
}

verify_match_mv "match_gov_lei" "lei" "gov_lei"
verify_match_mv "match_gov_ein" "ein" "gov_ein"

echo
echo "=== match_gov_ein: cluster-shape spot-check (intra-source duplication validates) ==="

# DOL Form 5500 has known intra-source duplication: 447,140 rows ein-bearing,
# 158,031 distinct EINs. So average cluster size ≈ 2.83. Top EIN by cluster size
# (366071399) should have ~1,435 members.

distinct_eins=$(dex_psql_query "SELECT COUNT(DISTINCT match_value)::int FROM entities.match_gov_ein")
assert_ge "distinct EINs in match_gov_ein (>= 150k)" "150000" "$distinct_eins"

multi_member_clusters=$(dex_psql_query "
  SELECT COUNT(*)::int FROM (
    SELECT match_value FROM entities.match_gov_ein GROUP BY match_value HAVING COUNT(*) > 1
  ) sub
")
assert_ge "EIN clusters with >1 member (>= 50k)" "50000" "$multi_member_clusters"

top_cluster_size=$(dex_psql_query "
  SELECT COUNT(*)::int FROM entities.match_gov_ein WHERE match_value = '366071399'
")
assert_ge "top EIN cluster (366071399) member count (>= 1000)" "1000" "$top_cluster_size"

echo
echo "=== match_gov_lei: every cluster is single-member in Phase 2 (only GLEIF has LEIs) ==="

# In Phase 2 lei is GLEIF's natural PK and no other source provides LEIs,
# so every cluster has exactly 1 member. When we add a second LEI-bearing
# source in a later phase, this number will rise.

multi_member_lei_clusters=$(dex_psql_query "
  SELECT COUNT(*)::int FROM (
    SELECT match_value FROM entities.match_gov_lei GROUP BY match_value HAVING COUNT(*) > 1
  ) sub
")
assert "LEI clusters with >1 member" "0" "$multi_member_lei_clusters"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
