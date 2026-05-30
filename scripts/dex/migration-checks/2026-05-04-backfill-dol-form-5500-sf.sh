#!/usr/bin/env bash
# Verifier for migration 20260504043136_backfill_dol_form_5500_sf_into_raw_entity_records.sql
#
# Phase 3a: validates the cross-source-EIN-match story. After this
# migration, raw_entity_records contains rows from both dol_form_5500
# AND dol_form_5500_sf, and match_gov_ein clusters now span both
# source_name values for shared EINs.

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

echo "=== raw_entity_records: dol_form_5500_sf backfill row-count parity ==="

source_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.source_dol_form_5500_sf")
promoted_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500_sf'")
assert "1:1 mirror: dol_form_5500_sf source rows = promoted rows" "$source_count" "$promoted_count"

orphans=$(dex_psql_query "
  SELECT COUNT(*)::bigint FROM entities.raw_entity_records r
  LEFT JOIN entities.source_dol_form_5500_sf s ON s.ack_id = r.source_row_id
  WHERE r.source_name = 'dol_form_5500_sf' AND s.ack_id IS NULL
")
assert "promoted dol_form_5500_sf rows with no matching source row" "0" "$orphans"

echo
echo "=== match_gov_ein: refreshed and now multi-source ==="

ein_total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_gov_ein")
assert_ge "match_gov_ein total rows (>= 1.5M after refresh)" "1500000" "$ein_total"

distinct_sources_in_ein=$(dex_psql_query "
  SELECT COUNT(DISTINCT r.source_name)::int
  FROM entities.match_gov_ein m
  JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
")
assert "match_gov_ein covers >= 2 distinct source_name values" "2" "$distinct_sources_in_ein"

echo
echo "=== match_gov_ein: cross-source clusters exist (the unlock) ==="

# A "cross-source cluster" is an EIN whose member raw_entity_records come
# from > 1 distinct source_name. This is the entity-resolution unlock that
# Phase 3a is designed to demonstrate.

cross_source_clusters=$(dex_psql_query "
  WITH cluster_sources AS (
    SELECT m.match_value, COUNT(DISTINCT r.source_name) AS distinct_sources
    FROM entities.match_gov_ein m
    JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
    GROUP BY m.match_value
  )
  SELECT COUNT(*)::bigint FROM cluster_sources WHERE distinct_sources >= 2
")
assert_ge "EIN clusters spanning >=2 source_name values (the cross-source unlock)" "10000" "$cross_source_clusters"

# Spot-check a known cross-source EIN: any EIN that's in BOTH dol_form_5500 AND dol_form_5500_sf
# Using SELECT INTERSECT logic to find one.
sample_cross=$(dex_psql_query "
  SELECT match_value FROM (
    SELECT DISTINCT match_value FROM entities.match_gov_ein m
    JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
    WHERE r.source_name = 'dol_form_5500'
    INTERSECT
    SELECT DISTINCT match_value FROM entities.match_gov_ein m
    JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
    WHERE r.source_name = 'dol_form_5500_sf'
  ) sub LIMIT 1
")

if [[ -n "$sample_cross" ]]; then
  echo "PASS: found a sample cross-source EIN (match_value=$sample_cross)"
  PASS_COUNT=$((PASS_COUNT + 1))

  cluster_breakdown=$(dex_psql_query "
    SELECT r.source_name||':'||COUNT(*)::text
    FROM entities.match_gov_ein m
    JOIN entities.raw_entity_records r ON r.raw_entity_id = m.raw_entity_id
    WHERE m.match_value = '$sample_cross'
    GROUP BY r.source_name
    ORDER BY r.source_name
  ")
  echo "      cluster breakdown for $sample_cross:"
  echo "$cluster_breakdown" | sed 's/^/        /'
else
  echo "FAIL: no cross-source EIN found — backfill or refresh did not produce cross-source clusters" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
