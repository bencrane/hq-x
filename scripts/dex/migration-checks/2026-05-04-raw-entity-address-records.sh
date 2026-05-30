#!/usr/bin/env bash
# Verifier for migration 20260504132254_create_raw_entity_address_records.sql
#
# Phase 5: addresses-as-sibling-table refactor with role tracking.

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

table_exists=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='entities' AND table_name='raw_entity_address_records'")
assert "raw_entity_address_records exists" "1" "$table_exists"

unique_constraint=$(dex_psql_query "SELECT 1 FROM information_schema.table_constraints WHERE table_schema='entities' AND table_name='raw_entity_address_records' AND constraint_name='raw_entity_address_records_unique'")
assert "UNIQUE (raw_entity_id, address_role) constraint" "1" "$unique_constraint"

dropped_count=$(dex_psql_query "SELECT COUNT(*)::int FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_records' AND column_name IN ('address_line1','address_line2','city','region','postal_code','country','lat','lon')")
assert "all 8 address-shaped columns dropped from raw_entity_records" "0" "$dropped_count"

echo
echo "=== per-source per-role row counts ==="

# FMCSA: physical + mailing
fmcsa_physical=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='fmcsa_motor_carriers' AND a.address_role='physical'")
assert_ge "fmcsa physical (>= 5M)" "5000000" "$fmcsa_physical"

fmcsa_mailing=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='fmcsa_motor_carriers' AND a.address_role='mailing'")
assert_ge "fmcsa mailing (>= 5M)" "5000000" "$fmcsa_mailing"

# DOL 5500: spons_dfe_mail_us is the most populated
dol_mail_us=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='dol_form_5500' AND a.address_role='spons_dfe_mail_us'")
assert_ge "dol_form_5500 spons_dfe_mail_us (>= 400k)" "400000" "$dol_mail_us"

# DOL 5500-SF: sf_spons_us
dol_sf_us=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='dol_form_5500_sf' AND a.address_role='sf_spons_us'")
assert_ge "dol_form_5500_sf sf_spons_us (>= 1.5M)" "1500000" "$dol_sf_us"

# DOL Sch C: provider_other_us
dol_sch_c=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='dol_form_5500_sch_c_providers' AND a.address_role='provider_other_us'")
assert_ge "dol_form_5500_sch_c_providers provider_other_us (>= 350k)" "350000" "$dol_sch_c"

# SBIR primary
sbir=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='sbir_awards' AND a.address_role='primary'")
assert_ge "sbir primary (>= 100k)" "100000" "$sbir"

# FINRA office + mailing
finra_office=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='finra_brokercheck_firms' AND a.address_role='office'")
assert_ge "finra office (>= 50k)" "50000" "$finra_office"

# GLEIF legal + hq (HQ might be sparse)
gleif_legal=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='gleif_lei_records' AND a.address_role='legal'")
assert_ge "gleif legal (>= 3M)" "3000000" "$gleif_legal"

gleif_hq=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id WHERE r.source_name='gleif_lei_records' AND a.address_role='hq'")
assert_ge "gleif hq (>= 100k)" "100000" "$gleif_hq"

echo
echo "=== overall: total + role distribution ==="

total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.raw_entity_address_records")
assert_ge "total address rows (>= 12M)" "12000000" "$total"

distinct_roles=$(dex_psql_query "SELECT COUNT(DISTINCT address_role)::int FROM entities.raw_entity_address_records")
assert_ge "distinct address_role values (>= 12)" "12" "$distinct_roles"

distinct_sources=$(dex_psql_query "SELECT COUNT(DISTINCT r.source_name)::int FROM entities.raw_entity_address_records a JOIN entities.raw_entity_records r ON r.raw_entity_id = a.raw_entity_id")
assert_ge "address rows span >= 7 sources" "7" "$distinct_sources"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
