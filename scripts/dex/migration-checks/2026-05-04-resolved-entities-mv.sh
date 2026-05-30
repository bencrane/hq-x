#!/usr/bin/env bash
# Verifier for migration 20260504153522_create_resolved_entities_mv.sql
#
# Phase 8: rule-based resolved_entities MV + priority config table +
# match_assertions_with_canonical view + resolved_entity_conflicts view.

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

priority_exists=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='entities' AND table_name='match_method_priority'")
assert "match_method_priority table exists" "1" "$priority_exists"

priority_rows=$(dex_psql_query "SELECT COUNT(*)::int FROM entities.match_method_priority")
assert "priority config has 5 methods seeded" "5" "$priority_rows"

priority_unique=$(dex_psql_query "SELECT 1 FROM information_schema.table_constraints WHERE table_schema='entities' AND table_name='match_method_priority' AND constraint_name='match_method_priority_unique'")
assert "priority UNIQUE constraint exists" "1" "$priority_unique"

base_view_exists=$(dex_psql_query "SELECT 1 FROM information_schema.views WHERE table_schema='entities' AND table_name='match_assertions_with_canonical'")
assert "match_assertions_with_canonical view exists" "1" "$base_view_exists"

mv_exists=$(dex_psql_query "SELECT 1 FROM pg_matviews WHERE schemaname='entities' AND matviewname='resolved_entities'")
assert "resolved_entities MV exists" "1" "$mv_exists"

mv_unique_idx=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='entities' AND tablename='resolved_entities' AND indexname='uq_resolved_entities_claim'")
assert "resolved_entities UNIQUE (raw_entity_id) index exists" "1" "$mv_unique_idx"

conflicts_view_exists=$(dex_psql_query "SELECT 1 FROM information_schema.views WHERE table_schema='entities' AND table_name='resolved_entity_conflicts'")
assert "resolved_entity_conflicts view exists" "1" "$conflicts_view_exists"

echo
echo "=== priority config: ordering check ==="

# Confirm priority order matches what we expect (lowest = highest precedence).
ordered=$(dex_psql_query "SELECT method FROM entities.match_method_priority ORDER BY priority ASC LIMIT 1")
assert "priority 1 = gov_lei" "gov_lei" "$ordered"

last=$(dex_psql_query "SELECT method FROM entities.match_method_priority ORDER BY priority DESC LIMIT 1")
assert "lowest priority = match_phone" "match_phone" "$last"

echo
echo "=== resolved_entities: row count + per-method coverage ==="

# Every claim that has at least one match assertion gets resolved.
mv_total=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities")
assert_ge "resolved_entities total rows (>= 12M)" "12000000" "$mv_total"

# Per-method counts. gov_lei has the most claims (3.3M GLEIF rows, all single-cluster),
# then gov_ein (2.4M from DOL), then match_phone (9.5M lower-priority).
gov_lei_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_lei'")
assert_ge "resolved via gov_lei (>= 3M)" "3000000" "$gov_lei_count"

gov_ein_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='gov_ein'")
assert_ge "resolved via gov_ein (>= 2M)" "2000000" "$gov_ein_count"

# match_phone resolves the residual claims that have no gov_lei/gov_ein/domain/email.
# That's a lot of FMCSA carriers without EINs.
phone_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entities WHERE resolved_via_method='match_phone'")
assert_ge "resolved via match_phone (>= 1M)" "1000000" "$phone_count"

echo
echo "=== resolved_entities: priority semantics check ==="

# Every gov_lei resolution has priority 10
gov_lei_pri=$(dex_psql_query "SELECT DISTINCT resolved_via_priority FROM entities.resolved_entities WHERE resolved_via_method='gov_lei'")
assert "gov_lei resolutions have priority 10" "10" "$gov_lei_pri"

gov_ein_pri=$(dex_psql_query "SELECT DISTINCT resolved_via_priority FROM entities.resolved_entities WHERE resolved_via_method='gov_ein'")
assert "gov_ein resolutions have priority 20" "20" "$gov_ein_pri"

# Critical invariant: when a claim has BOTH a gov_ein AND match_phone assertion,
# resolved_entities chooses gov_ein (priority 20 < 50). Verify by sampling.
priority_violations=$(dex_psql_query "
  SELECT COUNT(*)::bigint
  FROM entities.resolved_entities re
  WHERE EXISTS (
    SELECT 1 FROM entities.match_assertions_with_canonical mawc
    WHERE mawc.raw_entity_id = re.raw_entity_id
      AND mawc.priority < re.resolved_via_priority
  )
")
assert "no claim resolved by lower-precedence method when higher exists" "0" "$priority_violations"

echo
echo "=== resolved_entity_conflicts: surface check ==="

# Conflicts: claims with >1 method producing distinct canonicals. Should be
# substantial since match_phone and match_email both fire on lots of claims
# that ALSO have gov_ein.
conflict_count=$(dex_psql_query "SELECT COUNT(*)::bigint FROM entities.resolved_entity_conflicts")
assert_ge "conflict rows (>= 100k — many claims have multi-method evidence)" "100000" "$conflict_count"

# Spot-check: a known cross-method claim should appear in conflicts.
# Pick any GLEIF claim that ALSO has a phone assertion (rare since GLEIF has no
# phones; we rely on EIN/phone overlap from DOL instead). Use any DOL 5500 claim
# with both gov_ein and match_phone.
dol_with_both=$(dex_psql_query "
  SELECT COUNT(*)::bigint FROM (
    SELECT 1 FROM entities.resolved_entity_conflicts c
    JOIN entities.raw_entity_records r ON r.raw_entity_id = c.raw_entity_id
    WHERE r.source_name = 'dol_form_5500'
      AND 'gov_ein' = ANY(c.methods_by_priority)
      AND 'match_phone' = ANY(c.methods_by_priority)
    LIMIT 1
  ) sub
")
assert_ge "found at least one DOL claim with gov_ein + match_phone in conflicts" "1" "$dol_with_both"

echo
echo "=== summary ==="
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
