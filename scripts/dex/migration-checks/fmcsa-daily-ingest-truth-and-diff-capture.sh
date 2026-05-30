#!/usr/bin/env bash
# Verification harness for /scope cycle fmcsa-daily-ingest-truth-and-diff-capture.
#
# Authored by Stage 3.A audit subagent (2026-05-12 UTC) per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-12-fmcsa-daily-ingest-truth-and-diff-capture.md.
#
# Mirrors prior cycle patterns (fmcsa-pipeline-remediation.sh; sba-bridges-to-lance.sh).
# Single-quote surface bodies so $VAR / $(...) defer to the doppler-injected subshell.
# DEX checks via apps/data-engine-x/scripts/_lib/dex.sh.
#
# Usage:
#   ./fmcsa-daily-ingest-truth-and-diff-capture.sh                    # all surfaces
#   ./fmcsa-daily-ingest-truth-and-diff-capture.sh --surface s3       # single surface
#   ./fmcsa-daily-ingest-truth-and-diff-capture.sh --surface s0       # pre-flight only
#   MERGE_SHA=<sha> ./fmcsa-daily-ingest-truth-and-diff-capture.sh    # include s8 deploy gate

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying fmcsa-daily-ingest-truth-and-diff-capture (surface=${SURFACE_FILTER:-all})"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" cmd="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  echo "-- $id: RUNNING"
  if eval "$cmd"; then
    echo "-- $id: PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id: FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# ── s0: PRE-FLIGHT — required before any other surface ────────────────── #
# (1) R2_ENDPOINT non-empty.
# (2) FMCSA declaration row exists in ops.data_sources with at least 1 declaration.
# (3) Prior-lifecycle capture file exists at /tmp/dex-raw-landing-zone-lifecycle-prior.json
#     (s2 rollback requires it). The harness PRINTS A WARNING (does not fail)
#     because s2 has not run yet on first invocation; executor's process is to
#     capture-before-applying.
run_surface "s0" '
  ENDPOINT=$(_dex_doppler "echo \"\$R2_ENDPOINT\"") &&
  test -n "$ENDPOINT" &&
  test "${ENDPOINT#https://}" != "$ENDPOINT" &&
  DECL_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.material_attribute_declarations mad JOIN ops.data_sources ds ON ds.source_id = mad.source_id WHERE ds.display_name ILIKE '\''%fmcsa%'\''") &&
  test "$DECL_COUNT" -ge "1" &&
  echo "  s0: R2_ENDPOINT ok; $DECL_COUNT FMCSA material declarations registered" &&
  if [[ ! -f /tmp/dex-raw-landing-zone-lifecycle-prior.json ]]; then
    echo "  s0 WARNING: /tmp/dex-raw-landing-zone-lifecycle-prior.json missing (run BEFORE s2 lifecycle changes)" >&2
  fi
'

# ── s1: pipeline-trace docs ──────────────────────────────────────────── #
run_surface "s1" '
  DOC="$HQ_ALL_ROOT/apps/data-engine-x/docs/fmcsa-daily-pipeline.md" &&
  test -f "$DOC" &&
  grep -q "fmcsa_factory_daily_app.py" "$DOC" &&
  grep -q "0 6 \* \* \*" "$DOC" &&
  grep -q "data-engine-x-fmcsa-factory-daily" "$DOC"
'

# ── s2: R2 lifecycle policy — 5 enumerated FMCSA prefixes, 90-day floor ─ #
# Verify: lifecycle config exists; ≥5 rules covering the 5 FMCSA prefixes;
# each covering rule has Expiration.Days >= 90.
# Pre-step: prior-config snapshot must be on disk at /tmp/...prior.json
# (rollback insurance — captured BEFORE s2 modified anything).
run_surface "s2" '
  test -f /tmp/dex-raw-landing-zone-lifecycle-prior.json ||
    { echo "  s2 FAIL: prior-config snapshot missing at /tmp/dex-raw-landing-zone-lifecycle-prior.json" >&2; exit 1; } &&
  _dex_doppler "aws s3api get-bucket-lifecycle-configuration --bucket dex-raw-landing-zone --endpoint-url \"\$R2_ENDPOINT\" | jq -e \".Rules | map(select(.Filter.Prefix | test(\\\"^(fmcsa|fmcsa-carrier-essentials|fmcsa-derived|polaris-warehouse/fmcsa|iceberg-warehouse/fmcsa)/\\\"))) | length >= 5\"" &&
  _dex_doppler "aws s3api get-bucket-lifecycle-configuration --bucket dex-raw-landing-zone --endpoint-url \"\$R2_ENDPOINT\" | jq -e \".Rules | map(select(.Filter.Prefix | test(\\\"^(fmcsa|fmcsa-carrier-essentials|fmcsa-derived|polaris-warehouse/fmcsa|iceberg-warehouse/fmcsa)/\\\")) | map(select(.Expiration.Days >= 90))) | length >= 5\"" &&
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/docs/r2-retention-policy.md"
'

# ── s3: resolver path fix — non-zero FMCSA material_change_events ─────── #
# Pre-step: ≥2 snapshots at the corrected path; else COLD-START state
# (the fix lands but diff returns 0 until 2nd factory cycle — print info,
# do not fail).
# Verify: the source code at material_change_detector.py:267 references
# 'fmcsa-derived/carrier_essentials/' (the corrected path), and the most
# recent successful detection_run_id has emitted ≥1 FMCSA event (DOT-numeric
# entity_ref). The numeric-only regex filters out non-FMCSA sources.
run_surface "s3" '
  grep -q "fmcsa-derived/carrier_essentials/snapshot=" \
    "$HQ_ALL_ROOT/apps/data-engine-x/app/services/material_change_detector.py" &&
  SNAP_COUNT=$(_dex_doppler "aws s3 ls s3://dex-raw-landing-zone/fmcsa-derived/carrier_essentials/ --endpoint-url \"\$R2_ENDPOINT\"" | wc -l | tr -d " ") &&
  echo "  s3: $SNAP_COUNT snapshot dirs at corrected path" &&
  if [[ "$SNAP_COUNT" -lt "2" ]]; then
    echo "  s3 INFO: cold-start (snapshots < 2); fix shipped but diff produces zero until 2nd factory cycle" >&2
    exit 0
  fi &&
  EVENT_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.material_change_events mce JOIN ops.material_detection_runs mdr USING (detection_run_id) WHERE mdr.status='\''succeeded'\'' AND mdr.started_at >= now() - interval '\''24 hours'\'' AND mce.entity_ref ~ '\''^[0-9]+$'\''") &&
  test "$EVENT_COUNT" -ge "1"
'

# ── s5: migration applied — composite index on material_change_events ── #
# Verify: idx_material_events_run_attribute exists on (detection_run_id, attribute_name).
run_surface "s5" '
  IDX=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND tablename='\''material_change_events'\'' AND indexname='\''idx_material_events_run_attribute'\''") &&
  test "$IDX" = "1"
'

# ── s6: verify_daily_ingest.py script exists + exits 0 ────────────────── #
run_surface "s6" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/verify_daily_ingest.py" &&
  test -f "$SCRIPT" &&
  _dex_doppler "cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && python scripts/fmcsa/verify_daily_ingest.py"
'

# ── s7: CLAUDE.md doc section ─────────────────────────────────────────── #
run_surface "s7" '
  CLAUDE_MD="$HQ_ALL_ROOT/apps/data-engine-x/CLAUDE.md" &&
  test -f "$CLAUDE_MD" &&
  grep -q "^## FMCSA daily ingest pipeline" "$CLAUDE_MD" &&
  grep -q "fmcsa_factory_daily_app.py" "$CLAUDE_MD"
'

# ── s8: Railway deploy SUCCESS + Modal app present ────────────────────── #
# Gated by MERGE_SHA env (otherwise skip; deploy hasn't run yet).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s8" '
    STATUS=$(_dex_doppler "cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && railway status --service data-engine-x --json | jq -r .latestDeployment.status") &&
    test "$STATUS" = "SUCCESS" &&
    DEPLOYED_SHA=$(_dex_doppler "cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && railway status --service data-engine-x --json | jq -r .latestDeployment.meta.commitHash") &&
    test "$DEPLOYED_SHA" = "$MERGE_SHA" &&
    _dex_doppler "modal app list --json | jq -e \".[] | select(.name == \\\"data-engine-x-material-change-cron\\\")\""
  '
else
  echo "-- s8: SKIPPED (set MERGE_SHA env to verify deploy)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s9: runtime probe + manual cycle trigger evidence ─────────────────── #
# Verify: DEX serves a non-trivial route + a detection_run has succeeded in
# the last hour (i.e., the manual `modal run` or the natural 6h cron tick
# happened and the resolver fix is live).
run_surface "s9" '
  verify_service_runtime data-engine-x "https://api.dataengine.run" &&
  RECENT=$(dex_psql_query "SELECT COUNT(*) FROM ops.material_detection_runs WHERE status='\''succeeded'\'' AND started_at >= now() - interval '\''1 hour'\''") &&
  test "$RECENT" -ge "1"
'

echo ""
echo "==> Result: $PASS_COUNT pass, $FAIL_COUNT fail, $SKIP_COUNT skip"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
