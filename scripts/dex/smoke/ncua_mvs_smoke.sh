#!/usr/bin/env bash
# NCUA MVs smoke test: 5 acceptance checks per directive 2026-05-02-ncua-credit-union-mvs.md
#
# Usage (run from any cwd; requires Doppler scope set in repo dir):
#   doppler run -- bash apps/data-engine-x/scripts/smoke/ncua_mvs_smoke.sh
#
# Exits 0 on full pass. Final line is `acceptance_pass_rate=<int>` for harness scoring.
# Each check is a single psql query against $DEX_DB_URL_DIRECT (must be set by Doppler).

set -u
set -o pipefail

if [[ -z "${DEX_DB_URL_DIRECT:-}" ]]; then
  echo "ERROR: DEX_DB_URL_DIRECT not set; run via 'doppler run -- bash <script>'." >&2
  echo "acceptance_pass_rate=0"
  exit 1
fi

PASS=0
TOTAL=5

check() {
  local name="$1"
  local sql="$2"
  local expected="$3"
  local actual
  actual="$(psql "$DEX_DB_URL_DIRECT" -tAc "$sql" 2>/dev/null || echo "ERR")"
  if [[ "$actual" == "$expected" ]]; then
    echo "  PASS  $name  (got=$actual)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $name  (got=$actual expected=$expected)"
  fi
}

echo "NCUA MVs smoke test"
echo "==================="

# Check 1: both MVs exist in entities schema.
check "mvs_exist" \
  "SELECT count(*) FROM pg_matviews WHERE schemaname='entities' AND matviewname IN ('mv_ncua_credit_union_targeting','mv_ncua_signal_delta_distress');" \
  "2"

# Check 2: targeting MV row count in sanity band 4000..5000.
# Grain is (cu_number) from latest cycle_date — ~4,519 distinct CUs as of 2025-Q4.
# (Validator corrected the directive's 17,000-19,000 band which assumed per-quarter grain.)
check "targeting_rowcount_band" \
  "SELECT (count(*) BETWEEN 4000 AND 5000)::int FROM entities.mv_ncua_credit_union_targeting;" \
  "1"

# Check 3: signal-delta MV is queryable (returns non-negative integer; zero is OK).
check "distress_queryable" \
  "SELECT (count(*) >= 0)::int FROM entities.mv_ncua_signal_delta_distress;" \
  "1"

# Check 4: charter-number uniqueness in targeting MV.
check "charter_number_unique" \
  "SELECT (count(*) = count(DISTINCT charter_number))::int FROM entities.mv_ncua_credit_union_targeting;" \
  "1"

# Check 5: signal-delta MV has UNIQUE INDEX on dedup_hash (required for REFRESH CONCURRENTLY).
check "distress_dedup_hash_unique_index" \
  "SELECT count(*) FROM pg_indexes WHERE schemaname='entities' AND tablename='mv_ncua_signal_delta_distress' AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%dedup_hash%';" \
  "1"

PCT=$(( (PASS * 100) / TOTAL ))
echo
echo "passed=$PASS/$TOTAL"
echo "acceptance_pass_rate=$PCT"

[[ "$PASS" -eq "$TOTAL" ]] && exit 0 || exit 1
