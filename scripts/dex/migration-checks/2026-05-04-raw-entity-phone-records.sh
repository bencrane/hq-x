#!/usr/bin/env bash
# Verifier for migration 20260504125055_create_raw_entity_phone_records_and_match_phone.sql
#
# Phase 4: validates the phones-as-sibling-table refactor.
#   - raw_entity_phone_records exists with FK + UNIQUE
#   - phone column dropped from raw_entity_records
#   - per-source per-role row counts populated
#   - match_phone MV exists, CONCURRENTLY-refresh-ready, populated

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

table_exists=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='entities' AND table_name='raw_entity_phone_records'")
assert "raw_entity_phone_records exists" "1" "$table_exists"

unique_constraint=$(dex_psql_query "SELECT 1 FROM information_schema.table_constraints WHERE table_schema='entities' AND table_name='raw_entity_phone_records' AND constraint_name='raw_entity_phone_records_unique'")
assert "UNIQUE (raw_entity_id, phone, phone_role) constraint exists" "1" "$unique_constraint"

fk_exists=$(dex_psql_query "SELECT 1 FROM information_schema.referential_constraints WHERE constraint_schema='entities' AND constraint_name LIKE '%raw_entity_phone_records%'")
assert "FK to raw_entity_records exists" "1" "$fk_exists"

phone_col_dropped=$(dex_psql_query "SELECT COUNT(*)::int FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records' AND column_name='phone'")
assert "phone column dropped from raw_entity_records" "0" "$phone_col_dropped"

mv_exists=$(dex_psql_query "SELECT 1 FROM pg_matviews WHERE schemaname='entities' AND matviewname='match_phone'")
assert "match_phone MV exists" "1" "$mv_exists"

mv_unique_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='match_phone' AND indexname='uq_match_phone_claim_value'")
assert "match_phone UNIQUE (raw_entity_id, match_value) index exists" "1" "$mv_unique_idx"

echo
echo "=== raw_entity_phone_records: per-source per-role row counts ==="

# FMCSA telephone — close to 6.34M (rows where telephone IS NOT NULL AND <> '')
fmcsa_telephone=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='fmcsa_motor_carriers' AND p.phone_role='telephone'")
assert_ge "fmcsa telephone rows (>= 6M)" "6000000" "$fmcsa_telephone"

# FMCSA cell_phone — much sparser
fmcsa_cell=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='fmcsa_motor_carriers' AND p.phone_role='cell_phone'")
assert_ge "fmcsa cell_phone rows (>= 1k — likely sparse)" "1000" "$fmcsa_cell"

# DOL 5500 sponsor phone — close to 444K
dol_sponsor=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='dol_form_5500' AND p.phone_role='spons_dfe_phone_num'")
assert_ge "dol_form_5500 sponsor phone rows (>= 400k)" "400000" "$dol_sponsor"

# DOL 5500-SF sponsor phone — close to 1.57M
dol_sf_sponsor=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='dol_form_5500_sf' AND p.phone_role='sf_spons_phone_num'")
assert_ge "dol_form_5500_sf sponsor phone rows (>= 1.5M)" "1500000" "$dol_sf_sponsor"

# SBIR contact_phone — ~23K
sbir_contact=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='sbir_awards' AND p.phone_role='contact_phone'")
assert_ge "sbir contact_phone rows (>= 20k)" "20000" "$sbir_contact"

# SBIR pi_phone + ri_poc_phone — additional roles, varying sparsity
sbir_pi=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='sbir_awards' AND p.phone_role='pi_phone'")
assert_ge "sbir pi_phone rows (>= 100, validates the role exists)" "100" "$sbir_pi"

# FINRA business_phone_number — sparse (~3.2K)
finra=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_phone_records p JOIN entities.raw_entity_records r ON r.raw_entity_id = p.raw_entity_id WHERE r.source_name='finra_brokercheck_firms' AND p.phone_role='business_phone_number'")
assert_ge "finra business_phone_number rows (>= 1k)" "1000" "$finra"

echo
echo "=== match_phone MV: row count + cluster shape ==="

mv_rows=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.match_phone")
assert_ge "match_phone total rows (>= 5M)" "5000000" "$mv_rows"

# Distinct match_value count — proxies "how many distinct US phones we see"
distinct_phones=$(dex_psql_query "SELECT COUNT(DISTINCT match_value)::int FROM entities.match_phone")
assert_ge "match_phone distinct phone values (>= 1M)" "1000000" "$distinct_phones"

# Multi-member clusters — phones used by >1 entity_record
multi_member=$(dex_psql_query "
  SELECT COUNT(*)::int FROM (
    SELECT match_value FROM entities.match_phone GROUP BY match_value HAVING COUNT(*) > 1
  ) sub
")
assert_ge "match_phone clusters with >1 member (>= 100k)" "100000" "$multi_member"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
