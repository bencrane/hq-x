#!/usr/bin/env bash
# Verification harness for /scope cycle fmcsa-follow-on-cleanup-and-instrumentation.
#
# Authored by Stage 3.A audit subagent (2026-05-13 UTC) per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-13-fmcsa-follow-on-cleanup-and-instrumentation.md.
#
# Single-quote surface bodies so $VAR / $(...) defer to the doppler-injected subshell.
# DEX checks via apps/data-engine-x/scripts/_lib/dex.sh.
#
# Usage:
#   ./fmcsa-follow-on-cleanup-and-instrumentation.sh                    # all surfaces
#   ./fmcsa-follow-on-cleanup-and-instrumentation.sh --surface s3       # single surface
#   MERGE_SHA=<sha> ./fmcsa-follow-on-cleanup-and-instrumentation.sh    # include s8 deploy gate

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
# Honor a pre-set HQ_ALL_ROOT (worktree mode); else fall back to canonical lookup.
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
else
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"

# Canonical FMCSA source row + Telegram recipient (from validator pre-flight).
# shellcheck disable=SC2034
FMCSA_SOURCE_ID="3a24978f-3a80-4a7f-928a-fc9fed290f54"
# shellcheck disable=SC2034
TELEGRAM_RECIPIENT="1766428207"

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying fmcsa-follow-on-cleanup-and-instrumentation (surface=${SURFACE_FILTER:-all})"

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

# ── s0: PRE-FLIGHT ──────────────────────────────────────────────────── #
run_surface "s0" '
  ENDPOINT=$(_dex_doppler "echo \"\$R2_ENDPOINT\"") &&
  test -n "$ENDPOINT" &&
  test "${ENDPOINT#https://}" != "$ENDPOINT" &&
  SOURCE_OK=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE source_id = '\''$FMCSA_SOURCE_ID'\''") &&
  test "$SOURCE_OK" = "1" &&
  echo "  s0: R2_ENDPOINT + canonical FMCSA source_id present"
'

# ── s1: legacy path call sites updated to canonical ────────────────────── #
# Each of the 5 actionable files now references `fmcsa-derived/carrier_essentials/`
# (canonical) for its primary read path. Modal app identity names + the
# legitimate-legacy WRITER (build_fmcsa_carrier_essentials.py) are out-of-scope.
run_surface "s1" '
  for path in \
    "$HQ_ALL_ROOT/apps/data-engine-x/risingwave/fmcsa_pdl_domain_bridge.sql" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/apply_fmcsa_pdl_match_rw.py" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_fmcsa_carrier_emails_attributed.py" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_fmcsa_carrier_officers_normalized.py"; do
    grep -q "fmcsa-derived/carrier_essentials" "$path" || {
      echo "  s1 FAIL: $path missing canonical path reference" >&2
      exit 1
    }
  done &&
  echo "  s1: 4 of 5 file references confirmed canonical; init_polaris LOG name preserved as identity"
'

# ── s2: Modal daily verify app file present + parseable + cron ───────── #
run_surface "s2" '
  APP="$HQ_ALL_ROOT/apps/data-engine-x/modal/fmcsa_daily_verify_app.py" &&
  test -f "$APP" &&
  python3 -c "import ast; ast.parse(open(\"$APP\").read())" &&
  grep -q "modal.Cron(\"30 7 \* \* \*\")" "$APP" &&
  grep -q "dex-db" "$APP" &&
  grep -q "data_source_ingest_runs" "$APP"
'

# ── s3: material attribute coverage script present + parseable ────────── #
run_surface "s3" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/material_attribute_coverage_check.py" &&
  test -f "$SCRIPT" &&
  python3 -c "import ast; ast.parse(open(\"$SCRIPT\").read())" &&
  grep -q "material_attribute_declarations" "$SCRIPT" &&
  grep -q "material_change_events" "$SCRIPT"
'

# ── s4: Modal weekly coverage app present + parseable + cron ──────────── #
run_surface "s4" '
  APP="$HQ_ALL_ROOT/apps/data-engine-x/modal/fmcsa_weekly_coverage_app.py" &&
  test -f "$APP" &&
  python3 -c "import ast; ast.parse(open(\"$APP\").read())" &&
  grep -q "modal.Cron(\"0 12 \* \* 1\")" "$APP" &&
  grep -q "material_attribute_coverage_check" "$APP"
'

# ── s5: cost observability script present + parseable + acknowledges Modal-no-stats #
run_surface "s5" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/cost_observability/modal_trigger_billing_pull.py" &&
  test -f "$SCRIPT" &&
  python3 -c "import ast; ast.parse(open(\"$SCRIPT\").read())" &&
  grep -q "modal app history" "$SCRIPT" &&
  grep -q "no .modal app stats." "$SCRIPT" || grep -q "no public billing" "$SCRIPT"
'

# ── s6: cost observability docs ───────────────────────────────────────── #
run_surface "s6" '
  DOC="$HQ_ALL_ROOT/apps/data-engine-x/docs/cost-observability.md" &&
  test -f "$DOC" &&
  grep -qi "trigger.dev" "$DOC" &&
  grep -qi "modal" "$DOC" &&
  grep -q "modal_trigger_billing_pull.py" "$DOC"
'

# ── s7: alert_subscriptions rows present (idempotent INSERTs) ────────── #
run_surface "s7" '
  ROWS=$(dex_psql_query "SELECT COUNT(*) FROM ops.alert_subscriptions WHERE source_id = '\''$FMCSA_SOURCE_ID'\'' AND channel = '\''telegram'\'' AND recipient = '\''$TELEGRAM_RECIPIENT'\'' AND alert_kind IN ('\''ingest_failed'\'','\''cohort_drift'\'')") &&
  test "$ROWS" -ge "2" &&
  echo "  s7: $ROWS alert_subscriptions rows for FMCSA telegram routing"
'

# ── s8: Railway deploy SUCCESS + Modal apps deployed ─────────────────── #
# Gated by MERGE_SHA env (otherwise skip; deploy hasn't run yet).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s8" '
    STATUS=$(_dex_doppler "cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && railway status --service data-engine-x --json | jq -r .latestDeployment.status") &&
    test "$STATUS" = "SUCCESS" &&
    DEPLOYED_SHA=$(_dex_doppler "cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && railway status --service data-engine-x --json | jq -r .latestDeployment.meta.commitHash") &&
    test "$DEPLOYED_SHA" = "$MERGE_SHA" &&
    _dex_doppler "modal app list --json | jq -e \".[] | select(.name == \\\"data-engine-x-fmcsa-daily-verify\\\")\"" &&
    _dex_doppler "modal app list --json | jq -e \".[] | select(.name == \\\"data-engine-x-fmcsa-weekly-coverage\\\")\""
  '
else
  echo "-- s8: SKIPPED (set MERGE_SHA env to verify deploy)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s9: runtime probe + Modal invocation evidence ────────────────────── #
run_surface "s9" '
  verify_service_runtime data-engine-x "https://api.dataengine.run" &&
  HEARTBEAT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_source_ingest_runs WHERE source_id = '\''$FMCSA_SOURCE_ID'\'' AND run_metadata ->> '\''writer'\'' = '\''fmcsa-daily-verify'\'' AND started_at >= now() - interval '\''24 hours'\''") &&
  test "$HEARTBEAT" -ge "1" &&
  echo "  s9: DEX runtime probe OK; verify-cron heartbeat row present in last 24h"
'

echo ""
echo "==> Result: $PASS_COUNT pass, $FAIL_COUNT fail, $SKIP_COUNT skip"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
exit 0
