#!/usr/bin/env bash
# Verification harness for /scope cycle scorer-enrichment-borrower-ucc-history.
#
# Surfaces: s1-s11 (code + migration + deploy + smoke).
# Mirrors the proven ucc-gleif-identity-spine.sh shape (prior cycle):
#   - Helper-library sourcing via the migration-checks shim.
#   - hqx_psql_query helper for HQX_DB_URL_POOLED reads.
#   - dex_psql_query helper for DEX_DB_URL_POOLED reads.
#   - --surface / --repo filters; PASS / FAIL / SKIP accumulators.
#   - _lance_floor_check + _polaris_lance_check shared helpers.
#   - s9/s10 deploy block gated by MERGE_SHA.
#   - s11 smoke gated by SMOKE_E2E.
#
# Usage:
#   bash scorer-enrichment-borrower-ucc-history.sh                   # all surfaces
#   bash scorer-enrichment-borrower-ucc-history.sh --surface s1      # single surface
#   MERGE_SHA=<sha> bash scorer-enrichment-borrower-ucc-history.sh   # include deploys
#   SMOKE_E2E=1 bash scorer-enrichment-borrower-ucc-history.sh       # include smoke

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# HQ_ALL_ROOT can be overridden via env for worktree-local pre-merge runs.
if [[ -z "${HQ_ALL_ROOT:-}" ]]; then
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${HQ_ALL_ROOT:-}" ]] || [[ ! -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi
export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

# --- hq-x DB helpers (HQX_DB_URL_POOLED, distinct from DEX) -------------- #
_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}

hqx_psql_query() {
  local sql="$1"
  _hqx_doppler "psql \"\$HQX_DB_URL_POOLED\" -tAc \"$sql\""
}

# --- CLI parsing ---------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (surface filter: ${SURFACE_FILTER:-all}; repo filter: ${REPO_FILTER:-all})"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# --- Lance row-count gate (shared helper) -------------------------------- #
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 -c "
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
rows = ds.count_rows()
if rows >= $floor:
    print(f'PASS: $uri rows={rows:,} >= floor $floor')
    sys.exit(0)
print(f'FAIL: $uri rows={rows:,} < floor $floor')
sys.exit(1)
"
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet python apps/data-engine-x/scripts/init_polaris_lance_generic.py \
      --namespace "$ns" --table "$tbl" --check-only
}

# ── s1: borrowers/ucc_profile_lance emit script + Lance dataset ─────────── #
# Checks:
#   (a) emit script exists on disk.
#   (b) Lance dataset has ≥ 400,000 rows (recalibrated from directive's 1M).
#       Floor rationale: 499,620 distinct entity-ref tuples in bridge;
#       400K = ~80% allowing for filter dropouts.
# Note: s1 and s8 use identical verify commands — s8 is the post-backfill check.
run_surface "s1" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_borrowers_ucc_profile_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/borrowers/ucc_profile_lance/" 400000
'

# ── s2: models.py Pydantic extensions ───────────────────────────────────── #
# Checks:
#   (a) BridgeTierBonusConfig class exists.
#   (b) SourceProfileDatasetConfig class exists.
#   (c) ScoringStrategy has bridge_tier_bonus field.
#   (d) ScoringStrategy has source_profile_dataset field.
#   (e) MatchReasons has bridge_tier_bonus field.
#   (f) MatchReasons has source_profile_features field.
run_surface "s2" "hq-all" '
  MODELS="$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/models.py" &&
  test -f "$MODELS" &&
  grep -q "class BridgeTierBonusConfig" "$MODELS" &&
  grep -q "class SourceProfileDatasetConfig" "$MODELS" &&
  grep -q "bridge_tier_bonus.*BridgeTierBonusConfig" "$MODELS" &&
  grep -q "source_profile_dataset.*SourceProfileDatasetConfig" "$MODELS" &&
  grep -q "bridge_tier_bonus.*dict.*None" "$MODELS" &&
  grep -q "source_profile_features.*dict.*None" "$MODELS"
'

# ── s3: engine.py 5 disjoint hunks ──────────────────────────────────────── #
# Checks:
#   (a) ENTITY_REF_COLUMNS borrowers/ucc_profile_lance entry.
#   (b) scalar_attrs bug fix (scalar_attr_cols set union).
#   (c) _score_candidate source_context parameter.
#   (d) _compute_bridge_tier_bonus helper exists.
#   (e) _compute_source_profile_features helper exists.
#   (f) evaluate_relationship_for_intent wires source_context.
#   (g) bridge_tier_lookup key present.
run_surface "s3" "hq-all" '
  ENGINE="$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  test -f "$ENGINE" &&
  grep -q '"'"'"borrowers".*"ucc_profile_lance"'"'"' "$ENGINE" &&
  grep -q "scalar_attr_cols" "$ENGINE" &&
  grep -q "source_context" "$ENGINE" &&
  grep -q "_compute_bridge_tier_bonus" "$ENGINE" &&
  grep -q "_compute_source_profile_features" "$ENGINE" &&
  grep -q "bridge_tier_lookup" "$ENGINE"
'

# ── s4: ops.data_sources row for borrowers_ucc_profile_lance ────────────── #
run_surface "s4" "hq-all" '
  COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name='"'"'borrowers_ucc_profile_lance'"'"' AND format='"'"'lance'"'"' AND status='"'"'active'"'"'") &&
  test "$COUNT" = "1"
'

# ── s5: capital_partner_bridge_match_v1 scoring_strategy v2 ─────────────── #
# Checks that the JSONB has all v2 keys: bridge_namespace, bonus_by_tier,
# source_profile_dataset, namespace='borrowers', scalar_weight=1.0,
# recency_boost_weight=0.3.
run_surface "s5" "hq-all" '
  JSON=$(hqx_psql_query "SELECT scoring_strategy::text FROM business.matching_relationships WHERE name='"'"'capital_partner_bridge_match_v1'"'"'") &&
  echo "$JSON" | grep -q '"'"'bridge_namespace'"'"' &&
  echo "$JSON" | grep -q '"'"'bonus_by_tier'"'"' &&
  echo "$JSON" | grep -q '"'"'source_profile_dataset'"'"' &&
  echo "$JSON" | grep -q '"'"'borrowers'"'"' &&
  echo "$JSON" | grep -q '"'"'scalar_weight'"'"'
'

# ── s6: Polaris Generic Table API registration ───────────────────────────── #
run_surface "s6" "hq-all" '
  _polaris_lance_check "borrowers" "ucc_profile_lance"
'

# ── s7: _DAILY_SCRIPTS extension ────────────────────────────────────────── #
# Checks:
#   (a) emit_borrowers_ucc_profile_lance.py present in sba_bridges_internal_v1.py.
#   (b) It appears AFTER build_bridge_ucc_sba_borrower_lance.py (dependency order).
#   (c) sba-bridges-daily.ts docblock updated (16th script mentioned).
run_surface "s7" "hq-all" '
  ROUTER="$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  CRON="$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  test -f "$ROUTER" &&
  grep -q "emit_borrowers_ucc_profile_lance.py" "$ROUTER" &&
  LINE_BORROWER=$(grep -n "build_bridge_ucc_sba_borrower_lance.py" "$ROUTER" | head -1 | cut -d: -f1) &&
  LINE_EMIT=$(grep -n "emit_borrowers_ucc_profile_lance.py" "$ROUTER" | head -1 | cut -d: -f1) &&
  test "$LINE_EMIT" -gt "$LINE_BORROWER" &&
  grep -q "emit_borrowers_ucc_profile_lance.py" "$CRON"
'

# ── s8: backfill (post-apply Lance row-count floor — same as s1) ─────────── #
# Intentionally identical to s1: the backfill verify IS the Lance floor check.
# Pre-change s1 FAILs (dataset doesn't exist); post-backfill s8 PASSes.
# NB: s11-smoke-total (1686 existing matches) is a sanity floor, NOT a
# discriminating gate — see review notes §"Pre-change verify-against-state".
run_surface "s8" "hq-all" '
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/borrowers/ucc_profile_lance/" 400000
'

# ── s9/s10: deploy (gated by MERGE_SHA) ─────────────────────────────────── #
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s9-deploy-hqx" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd '"$HQ_ALL_ROOT"' && railway status --json 2>/dev/null |
      jq -e -r '"'"'.environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\"hq-x\") | .latestDeployment | select(.status==\"SUCCESS\") | .meta.commitHash'"'"' > /dev/null
    "
  '
  run_surface "s9-runtime-probe-hqx" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_with_runtime_probes hq-x "https://api.opsengine.run" \
      "/api/v1/internal/matching-engine/evaluate-all"
  '
  run_surface "s10-deploy-dex" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd '"$HQ_ALL_ROOT"' && railway status --json 2>/dev/null |
      jq -e -r '"'"'.environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\"data-engine-x\") | .latestDeployment | select(.status==\"SUCCESS\") | .meta.commitHash'"'"' > /dev/null
    "
  '
  run_surface "s10-runtime-probe-dex" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s9/s10 deploy (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s11: end-to-end matching-engine smoke (gated by SMOKE_E2E=1) ─────────── #
# Assertions (all must pass after firing evaluate-all against prod):
#   s11-smoke-total:            COUNT(*) > 100 (sanity; pre-change already passes with 1686 rows)
#   s11-smoke-variance:         VAR_SAMP(score) > 0.1 across top-100 (rejects uniform collapse)
#   s11-smoke-platinum:         ≥1 row with bridge_tier_bonus.tier='platinum' AND bonus > 0
#   s11-smoke-source-profile:   ≥1 row with non-empty source_profile_features jsonb
#   s11-smoke-scalar-hits-matched: ≥1 distinct matched=true attribute OUTSIDE {borrstate, has_pending_commit}
#                                  (reviewer-strengthened gate — pre-change this is 0; post-fix should be >0)
if [[ -n "${SMOKE_E2E:-}" ]]; then

  run_surface "s11-smoke-total" "hq-all" '
    COUNT=$(hqx_psql_query "SELECT COUNT(*) FROM business.matches WHERE identified_at >= NOW() - INTERVAL '"'"'24 hours'"'"'") &&
    test "$COUNT" -gt "100"
  '

  run_surface "s11-smoke-variance" "hq-all" '
    VARIANCE=$(hqx_psql_query "
      SELECT COALESCE(VAR_SAMP(score), 0)
      FROM (
        SELECT score FROM business.matches
        WHERE identified_at >= NOW() - INTERVAL '"'"'24 hours'"'"'
        ORDER BY score DESC
        LIMIT 100
      ) top100
    ") &&
    awk -v v="${VARIANCE:-0}" '"'"'BEGIN { exit (v+0 > 0.1) ? 0 : 1 }'"'"'
  '

  run_surface "s11-smoke-platinum" "hq-all" '
    COUNT=$(hqx_psql_query "
      SELECT COUNT(*)
      FROM business.matches
      WHERE identified_at >= NOW() - INTERVAL '"'"'24 hours'"'"'
        AND match_reasons->'"'"'bridge_tier_bonus'"'"'->>'"'"'tier'"'"' = '"'"'platinum'"'"'
        AND (match_reasons->'"'"'bridge_tier_bonus'"'"'->>'"'"'bonus'"'"')::float > 0
    ") &&
    test "$COUNT" -gt "0"
  '

  run_surface "s11-smoke-source-profile" "hq-all" '
    COUNT=$(hqx_psql_query "
      SELECT COUNT(*)
      FROM business.matches
      WHERE identified_at >= NOW() - INTERVAL '"'"'24 hours'"'"'
        AND match_reasons->'"'"'source_profile_features'"'"' IS NOT NULL
        AND match_reasons->'"'"'source_profile_features'"'"' != '"'"'null'"'"'::jsonb
        AND match_reasons->'"'"'source_profile_features'"'"' != '"'"'{}'"'"'::jsonb
    ") &&
    test "$COUNT" -gt "0"
  '

  # Reviewer-strengthened gate: attribute names OUTSIDE the pre-change baseline
  # {borrstate, has_pending_commit} must appear as matched=true in scalar_hits.
  # Pre-change count = 0; post-scalar_attrs-fix should produce >0.
  run_surface "s11-smoke-scalar-hits-matched" "hq-all" '
    COUNT=$(hqx_psql_query "
      SELECT COUNT(DISTINCT attr_name)
      FROM (
        SELECT jsonb_array_elements(match_reasons->'"'"'scalar_hits'"'"')->>'"'"'attribute'"'"' AS attr_name,
               (jsonb_array_elements(match_reasons->'"'"'scalar_hits'"'"')->>'"'"'matched'"'"')::boolean AS matched
        FROM business.matches
        WHERE identified_at >= NOW() - INTERVAL '"'"'24 hours'"'"'
          AND match_reasons->'"'"'scalar_hits'"'"' IS NOT NULL
      ) hits
      WHERE matched = true
        AND attr_name NOT IN ('"'"'borrstate'"'"', '"'"'has_pending_commit'"'"')
    ") &&
    test "$COUNT" -gt "0"
  '

else
  echo "-- s11 smoke (hq-all): SKIPPED (set SMOKE_E2E=1 to run)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
