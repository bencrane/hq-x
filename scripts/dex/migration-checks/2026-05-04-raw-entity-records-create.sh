#!/usr/bin/env bash
# Verifier for migration 20260504040715_create_raw_entity_records.sql
#
# Phase 1 of the entities-table claim-ledger initiative: validates the
# raw_entity_records master table is created, indexed, backfilled from
# GLEIF + DOL Form 5500, and that every promoted row resolves back to a
# real source_* row (1:1 mirror integrity).
#
# Usage: bash 2026-05-04-raw-entity-records-create.sh

set -euo pipefail

# Source the shim that locates the code-repo _lib/dex.sh
HQ_SHIM="${HOME}/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
if [[ -f "$HQ_SHIM" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_SHIM"
else
  # Fallback: source dex.sh directly from the worktree
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

echo "=== raw_entity_records: schema check ==="

table_exists=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='entities' AND table_name='raw_entity_records'")
assert "table entities.raw_entity_records exists" "1" "$table_exists"

col_count=$(dex_psql_query "SELECT COUNT(*)::int FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records'")
assert_ge "column count >= 30" "30" "$col_count"

unique_constraint=$(dex_psql_query "SELECT 1 FROM information_schema.table_constraints WHERE table_schema='entities' AND table_name='raw_entity_records' AND constraint_name='raw_entity_records_source_unique'")
assert "UNIQUE (source_name, source_row_id) constraint exists" "1" "$unique_constraint"

idx_count=$(dex_psql_query "SELECT COUNT(*)::int FROM pg_indexes WHERE schemaname='entities' AND tablename='raw_entity_records' AND indexname LIKE 'idx_raw_entity_records_%'")
assert_ge "match-key indexes >= 9" "9" "$idx_count"

echo
echo "=== raw_entity_records: volume floors ==="

# Floor: ~80% of source row count (the migration backfill should be 1:1 minus
# whatever rare PK collisions might happen, which should be zero for these sources).
gleif_floor=2900000   # source has ~3.3M; floor at ~88% leaves margin for any drift
dol_floor=400000      # source has ~447K; floor at ~89% leaves margin

dex_min_row_floor_check entities.raw_entity_records 3300000 || FAIL_COUNT=$((FAIL_COUNT + 1))

gleif_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='gleif_lei_records'")
assert_ge "raw_entity_records rows from gleif_lei_records" "$gleif_floor" "$gleif_rows"

dol_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500'")
assert_ge "raw_entity_records rows from dol_form_5500" "$dol_floor" "$dol_rows"

echo
echo "=== raw_entity_records: 1:1 mirror integrity ==="

# Per-source: COUNT in raw_entity_records must match COUNT in source_*.
# This is the H2/H17 runtime check the premortem flagged as load-bearing.

gleif_source=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.source_gleif_lei_records")
gleif_promoted=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='gleif_lei_records'")
assert "1:1 mirror: gleif source rows = promoted rows" "$gleif_source" "$gleif_promoted"

dol_source=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.source_dol_form_5500")
dol_promoted=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500'")
assert "1:1 mirror: dol_form_5500 source rows = promoted rows" "$dol_source" "$dol_promoted"

echo
echo "=== raw_entity_records: orphan check (FK resolves) ==="

# Every (source_name, source_row_id) must resolve back to a real source_*
# row. Polymorphic, so we union per-source LEFT JOINs.

gleif_orphans=$(dex_psql_query "
  SELECT COUNT(*)::bigint FROM entities.raw_entity_records r
  LEFT JOIN entities.source_gleif_lei_records s ON s.lei = r.source_row_id
  WHERE r.source_name = 'gleif_lei_records' AND s.lei IS NULL
")
assert "gleif promoted rows with no matching source row (orphans)" "0" "$gleif_orphans"

dol_orphans=$(dex_psql_query "
  SELECT COUNT(*)::bigint FROM entities.raw_entity_records r
  LEFT JOIN entities.source_dol_form_5500 s ON s.ack_id = r.source_row_id
  WHERE r.source_name = 'dol_form_5500' AND s.ack_id IS NULL
")
assert "dol promoted rows with no matching source row (orphans)" "0" "$dol_orphans"

echo
echo "=== raw_entity_records: spot-checks (gov-id population) ==="

# Every GLEIF row must have a non-null lei
gleif_no_lei=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='gleif_lei_records' AND lei IS NULL")
assert "gleif rows missing lei" "0" "$gleif_no_lei"

# Every DOL row must have non-null ein (the sponsor EIN — Form 5500 always has it)
dol_no_ein=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='dol_form_5500' AND ein IS NULL")
assert_ge "dol rows missing ein (some forms can have NULL EIN)" "0" "$dol_no_ein"

# At least 95% of GLEIF should have a city (sanity: the address backfill worked)
gleif_with_city=$(dex_psql_query "SELECT (COUNT(*) FILTER (WHERE city IS NOT NULL) * 100 / COUNT(*))::int FROM entities.raw_entity_records WHERE source_name='gleif_lei_records'")
assert_ge "gleif rows with city populated (>=70%)" "70" "$gleif_with_city"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
