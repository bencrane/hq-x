#!/usr/bin/env bash
# Verifier harness for /scope directive 2026-05-04-pdl-canonical-phase-14c
# (Phase 14c — promote entities.pdl_companies into the canonical claim-ledger
# spine; ENTITIES.md Recipe A).
#
# Surfaces:
#   s1  migration  CREATE entities.raw_entity_linkedin_records
#   s2  migration  INSERT pdl_companies → raw_entity_records
#   s3  migration  INSERT pdl websites → raw_entity_website_records
#   s4  migration  INSERT pdl linkedin_url → raw_entity_linkedin_records
#   s5  migration  REFRESH match_domain_etld_plus_one
#   s6  migration  REFRESH resolved_entities
#   s7  code       LEFT JOIN pdl_companies in get_canonical_profile
#   s8  code       MCP docstring update
#   s9  code       unit test added
#   s10 deploy     Railway data-engine-x SUCCESS at merge SHA
#   s11 endpoint   curl /profile returns PDL firmographics
#   s12 code       ENTITIES.md updated

set -euo pipefail

HQ_SHIM="${HOME}/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
if [[ -f "$HQ_SHIM" ]]; then
  # shellcheck source=/dev/null
  source "$HQ_SHIM"
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/Desktop/hq-all")"
  # shellcheck source=/dev/null
  source "$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
fi

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="bencrane/hq-all"
SKIP=0
if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$REPO" ]]; then
  SKIP=1
fi

PASS=0
FAIL=0
echo "==> Verifying surfaces (filter: ${REPO_FILTER:-all})"

q() { dex_psql_query "$1"; }

run() {
  local id="$1"; shift
  local fn="$1"; shift
  if [[ "$SKIP" -eq 1 ]]; then
    echo "-- $id ($REPO): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($REPO): RUNNING"
  if "$fn"; then
    echo "-- $id ($REPO): PASS"
    PASS=$((PASS + 1))
  else
    echo "-- $id ($REPO): FAIL" >&2
    FAIL=$((FAIL + 1))
  fi
}

verify_s1() {
  local exists cols uniq fk
  exists=$(q "SELECT 1 FROM pg_tables WHERE schemaname='entities' AND tablename='raw_entity_linkedin_records'")
  [[ "$exists" == "1" ]] || { echo "    table missing"; return 1; }
  cols=$(q "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='entities' AND table_name='raw_entity_linkedin_records'")
  echo "    cols=$cols"
  for c in raw_entity_linkedin_id raw_entity_id linkedin_url linkedin_role ingested_at; do
    echo "$cols" | grep -q "$c" || { echo "    missing col $c"; return 1; }
  done
  uniq=$(q "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='entities' AND t.relname='raw_entity_linkedin_records' AND c.contype='u'")
  [[ "$uniq" == "1" ]] || { echo "    missing UNIQUE constraint"; return 1; }
  fk=$(q "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='entities' AND t.relname='raw_entity_linkedin_records' AND c.contype='f'")
  [[ "$fk" == "1" ]] || { echo "    missing FK constraint"; return 1; }
}

verify_s2() {
  local n
  n=$(q "SELECT COUNT(*)::bigint FROM entities.raw_entity_records WHERE source_name='pdl_companies'")
  echo "    pdl claims: $n"
  (( n >= 35300000 )) || { echo "    FAIL: $n < 35.3M floor"; return 1; }
}

verify_s3() {
  local n
  n=$(q "SELECT COUNT(*)::bigint FROM entities.raw_entity_website_records w JOIN entities.raw_entity_records r ON r.raw_entity_id=w.raw_entity_id WHERE r.source_name='pdl_companies' AND w.website_role='website'")
  echo "    pdl-source website rows: $n"
  (( n >= 23000000 )) || { echo "    FAIL: $n < 23M floor"; return 1; }
}

verify_s4() {
  local n
  n=$(q "SELECT COUNT(*)::bigint FROM entities.raw_entity_linkedin_records WHERE linkedin_role='linkedin_url'")
  echo "    pdl linkedin rows: $n"
  (( n >= 35300000 )) || { echo "    FAIL: $n < 35.3M floor"; return 1; }
}

verify_s5() {
  local n
  n=$(q "SELECT COUNT(*)::bigint FROM entities.match_domain_etld_plus_one")
  echo "    match_domain rows: $n"
  (( n >= 5000000 )) || { echo "    FAIL: $n < 5M floor"; return 1; }
}

verify_s6() {
  local resolved raw
  resolved=$(q "SELECT COUNT(*)::bigint FROM entities.resolved_entities")
  raw=$(q "SELECT COUNT(*)::bigint FROM entities.raw_entity_records")
  echo "    resolved=$resolved raw=$raw"
  (( resolved >= raw / 2 )) || { echo "    FAIL: coverage <50% raw"; return 1; }
}

verify_s7() {
  local f="${HOME}/Desktop/hq-all/apps/data-engine-x/app/services/canonical_entities_query.py"
  grep -q "pdl_companies" "$f" || { echo "    no pdl_companies reference in $f"; return 1; }
  grep -qE "industry|locality|founded" "$f" || { echo "    no firmographic field reference"; return 1; }
}

verify_s8() {
  local f="${HOME}/Desktop/hq-all/apps/data-engine-x/app/mcp_server/tools/canonical_entities.py"
  for field in industry size founded locality region country; do
    grep -q "$field" "$f" || { echo "    docstring missing field: $field"; return 1; }
  done
}

verify_s9() {
  local f="${HOME}/Desktop/hq-all/apps/data-engine-x/tests/mcp_server/test_canonical_entities_mcp.py"
  grep -qE "test_entity_signals_consolidated_surfaces_pdl|test_.*pdl_firmographic" "$f" || { echo "    no PDL firmographic test"; return 1; }
}

verify_s10() {
  if ! command -v railway >/dev/null 2>&1; then
    echo "    railway CLI not installed; deploy-verifier handles post-merge"
    return 0
  fi
  echo "    (post-merge: deploy-verifier reads railway status)"
}

verify_s11() {
  echo "    (post-deploy only: deploy-verifier curls /profile against api.dataengine.run)"
}

verify_s12() {
  local f="${HOME}/Desktop/hq-all/apps/data-engine-x/ENTITIES.md"
  grep -q "raw_entity_linkedin_records" "$f" || { echo "    sibling table not in §Schemas"; return 1; }
  grep -q "pdl_companies" "$f" || { echo "    pdl_companies not in §Current state"; return 1; }
  grep -qE "14c" "$f" || { echo "    Phase 14c not in §Phases shipped"; return 1; }
}

run "s1" verify_s1
run "s2" verify_s2
run "s3" verify_s3
run "s4" verify_s4
run "s5" verify_s5
run "s6" verify_s6
run "s7" verify_s7
run "s8" verify_s8
run "s9" verify_s9
run "s10" verify_s10
run "s11" verify_s11
run "s12" verify_s12

echo ""
echo "==> PASS: $PASS  FAIL: $FAIL"
exit $((FAIL > 0))
