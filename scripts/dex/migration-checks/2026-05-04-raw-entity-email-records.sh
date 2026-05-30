#!/usr/bin/env bash
# Verifier for migration 20260504144843_create_raw_entity_email_records_and_match_email.sql
#
# Phase 6: emails-as-sibling-table refactor + match_email MV.

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

echo "=== schema check ==="

table_exists=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='entities' AND table_name='raw_entity_email_records'")
assert "raw_entity_email_records exists" "1" "$table_exists"

unique_constraint=$(dex_psql_query "SELECT 1 FROM information_schema.table_constraints WHERE table_schema='entities' AND table_name='raw_entity_email_records' AND constraint_name='raw_entity_email_records_unique'")
assert "UNIQUE (raw_entity_id, email, email_role) constraint" "1" "$unique_constraint"

email_dropped=$(dex_psql_query "SELECT COUNT(*)::int FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records' AND column_name='email'")
assert "email column dropped from raw_entity_records" "0" "$email_dropped"

mv_exists=$(dex_psql_query "SELECT 1 FROM pg_matviews WHERE schemaname='entities' AND matviewname='match_email'")
assert "match_email MV exists" "1" "$mv_exists"

mv_unique_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='match_email' AND indexname='uq_match_email_claim_value'")
assert "match_email UNIQUE (raw_entity_id, match_value) index exists" "1" "$mv_unique_idx"

echo
echo "=== per-source per-role row counts ==="

fmcsa=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_email_records e JOIN entities.raw_entity_records r ON r.raw_entity_id = e.raw_entity_id WHERE r.source_name='fmcsa_motor_carriers' AND e.email_role='email_address'")
assert_ge "fmcsa email_address (>= 4M)" "4000000" "$fmcsa"

sbir_contact=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_email_records e JOIN entities.raw_entity_records r ON r.raw_entity_id = e.raw_entity_id WHERE r.source_name='sbir_awards' AND e.email_role='contact_email'")
assert_ge "sbir contact_email (>= 20k)" "20000" "$sbir_contact"

sbir_pi=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_email_records e JOIN entities.raw_entity_records r ON r.raw_entity_id = e.raw_entity_id WHERE r.source_name='sbir_awards' AND e.email_role='pi_email'")
assert_ge "sbir pi_email (>= 100k)" "100000" "$sbir_pi"

echo
echo "=== match_email MV ==="

mv_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_email")
assert_ge "match_email total (>= 3M)" "3000000" "$mv_rows"

distinct_emails=$(dex_psql_query "SELECT COUNT(DISTINCT match_value)::int FROM entities.match_email")
assert_ge "match_email distinct values (>= 1M)" "1000000" "$distinct_emails"

multi_member=$(dex_psql_query "
  SELECT COUNT(*)::int FROM (
    SELECT match_value FROM entities.match_email GROUP BY match_value HAVING COUNT(*) > 1
  ) sub
")
assert_ge "match_email clusters with >1 member (>= 1k)" "1000" "$multi_member"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
