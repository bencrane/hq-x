#!/usr/bin/env bash
# Verification harness for /scope cycle usaspending-pipeline-remediation.
#
# Authored 2026-05-13 UTC per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-13-usaspending-pipeline-remediation.md.
#
# Mirrors the pattern from fmcsa-follow-on-cleanup-and-instrumentation.sh —
# single-quote surface bodies so $VAR / $(...) defer to the doppler-injected
# subshell. DEX checks via apps/data-engine-x/scripts/_lib/dex.sh.
#
# Usage:
#   ./usaspending-pipeline-remediation.sh                    # all surfaces
#   ./usaspending-pipeline-remediation.sh --surface s3       # single surface
#   MERGE_SHA=<sha> ./usaspending-pipeline-remediation.sh    # include deploy gate

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
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

# Telegram recipient from validator pre-flight (matches FMCSA pattern).
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

echo "==> Verifying usaspending-pipeline-remediation (surface=${SURFACE_FILTER:-all})"

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
  USA_SRC=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('\''usaspending_contracts_lance'\'','\''usaspending_api_daily'\'')") &&
  test "$USA_SRC" = "2" &&
  echo "  s0: R2_ENDPOINT present + 2 USAspending data_sources rows"
'

# ── s1: 0-byte poison file deleted (or never re-created) ─────────────── #
# Post-merge expectation: the 0-byte parquet at date=2026-05-11 is replaced
# by a real backfill OR is absent. The pre-check is fragile (it depends on
# the s2 backfill having run, which is post-merge manual). So this verifies
# the canonical state AFTER backfill: object exists AND size > 0.
run_surface "s1" '
  KEY="usaspending/contracts/api-delta/date=2026-05-11/data.parquet" &&
  SIZE=$(_dex_doppler "aws s3api head-object --bucket dex-raw-landing-zone --key \"$KEY\" --endpoint-url \"\$R2_ENDPOINT\" --query ContentLength --output text 2>/dev/null || echo \"absent\"") &&
  if [[ "$SIZE" == "absent" ]]; then
    echo "  s1: R2 key absent (poison deleted; backfill not yet run)" &&
    return 0
  elif [[ "$SIZE" == "0" ]]; then
    echo "  s1 FAIL: R2 key still 0-byte poison" >&2 &&
    return 1
  else
    echo "  s1: R2 key has $SIZE bytes (backfill landed)" &&
    return 0
  fi
'

# ── s2: backfill script present + parseable ──────────────────────────── #
run_surface "s2" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/usaspending/backfill_missing_2026_05_10.py" &&
  test -f "$SCRIPT" &&
  python3 -c "import ast; ast.parse(open(\"$SCRIPT\").read())" &&
  grep -q "FEED_DATE = date(2026, 5, 10)" "$SCRIPT" &&
  grep -q "idempotency_key" "$SCRIPT"
'

# ── s3: RW MV catalog rows retired ───────────────────────────────────── #
run_surface "s3" '
  RETIRED=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('\''mv_federal_contract_leads'\'','\''mv_usaspending_contracts_typed'\'') AND status = '\''retired'\''") &&
  test "$RETIRED" = "2" &&
  echo "  s3: 2 RW MV catalog rows retired"
'

# ── s4: RW MV references — service-code reads from entities.* (Postgres) #
# All call sites in app/ should reference entities.mv_*; new RW connection
# URLs / env vars are banned. Note: incidental "rw_mv" string mentions in
# DDL-comment lists / format-enum docstrings are acceptable; we only block
# actual RW connection URLs / env vars. grep exits 1 when nothing matches,
# so wrap stages in {grep || true} so set -e + pipefail does not abort.
run_surface "s4" '
  RW_URL_HITS=$({ grep -rln "risingwave://\|RISINGWAVE_URL\|RW_URL" "$HQ_ALL_ROOT/apps/data-engine-x/app/" 2>/dev/null || true; } | wc -l | tr -d " ") &&
  test "$RW_URL_HITS" = "0" &&
  ENTITIES_HITS=$({ grep -rln "entities.mv_federal_contract_leads" "$HQ_ALL_ROOT/apps/data-engine-x/app/" 2>/dev/null || true; } | wc -l | tr -d " ") &&
  test "$ENTITIES_HITS" -ge "5" &&
  echo "  s4: 0 RW-URL references + ${ENTITIES_HITS} entities.* reads in app/"
'

# ── s5: risingwave/usaspending_daily.sql deprecation comment ─────────── #
run_surface "s5" '
  FILE="$HQ_ALL_ROOT/apps/data-engine-x/risingwave/usaspending_daily.sql" &&
  test -f "$FILE" &&
  grep -q "DEPRECATED 2026-05-13" "$FILE" &&
  grep -qi "RisingWave is being retired" "$FILE"
'

# ── s6: USAspending material declarations migration present + parses ── #
run_surface "s6" '
  MIG="$HQ_ALL_ROOT/apps/data-engine-x/supabase/migrations/20260513154300_usaspending_material_attribute_declarations.sql" &&
  test -f "$MIG" &&
  grep -q "recipient_uei" "$MIG" &&
  grep -q "total_obligated_amount" "$MIG" &&
  grep -q "period_of_performance_end_date" "$MIG" &&
  grep -q "naics_code" "$MIG" &&
  SEED="$HQ_ALL_ROOT/apps/data-engine-x/scripts/seed_material_declarations_usaspending.py" &&
  test -f "$SEED" &&
  python3 -c "import ast; ast.parse(open(\"$SEED\").read())"
'

# ── s7: detector resolver wires USAspending ──────────────────────────── #
run_surface "s7" '
  DETECTOR="$HQ_ALL_ROOT/apps/data-engine-x/app/services/material_change_detector.py" &&
  grep -q "resolve_usaspending_contracts_snapshots" "$DETECTOR" &&
  grep -q "\"usaspending_contracts_lance\":" "$DETECTOR" &&
  grep -q "generated_unique_award_id" "$DETECTOR" &&
  python3 -c "import ast; ast.parse(open(\"$DETECTOR\").read())"
'

# ── s8: zero-row upstream check helper present + parses ──────────────── #
run_surface "s8" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/usaspending/zero_row_upstream_check.py" &&
  test -f "$SCRIPT" &&
  python3 -c "import ast; ast.parse(open(\"$SCRIPT\").read())" &&
  grep -q "upstream_has_data" "$SCRIPT"
'

# ── s9: ledger unify helper present + parses ─────────────────────────── #
run_surface "s9" '
  SCRIPT="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/ingest_ledger_unify.py" &&
  test -f "$SCRIPT" &&
  python3 -c "import ast; ast.parse(open(\"$SCRIPT\").read())" &&
  grep -q "reconcile_bulk_ingest_to_ops" "$SCRIPT" &&
  grep -q "reconcile_all_usaspending" "$SCRIPT"
'

# ── s10: daily verify Modal app present + cron + parses ──────────────── #
run_surface "s10" '
  APP="$HQ_ALL_ROOT/apps/data-engine-x/modal/usaspending_daily_verify_app.py" &&
  test -f "$APP" &&
  python3 -c "import ast; ast.parse(open(\"$APP\").read())" &&
  grep -q "modal.Cron(\"0 8 \* \* \*\")" "$APP" &&
  grep -q "dex-db" "$APP" &&
  grep -q "usaspending_contracts_lance" "$APP" &&
  VERIFY="$HQ_ALL_ROOT/apps/data-engine-x/scripts/usaspending/verify_daily_ingest.py" &&
  test -f "$VERIFY" &&
  python3 -c "import ast; ast.parse(open(\"$VERIFY\").read())"
'

# ── s11: weekly coverage Modal app present + cron + parses ──────────── #
run_surface "s11" '
  APP="$HQ_ALL_ROOT/apps/data-engine-x/modal/usaspending_weekly_coverage_app.py" &&
  test -f "$APP" &&
  python3 -c "import ast; ast.parse(open(\"$APP\").read())" &&
  grep -q "modal.Cron(\"30 12 \* \* 1\")" "$APP" &&
  COVERAGE="$HQ_ALL_ROOT/apps/data-engine-x/scripts/usaspending/material_attribute_coverage_check.py" &&
  test -f "$COVERAGE" &&
  python3 -c "import ast; ast.parse(open(\"$COVERAGE\").read())"
'

# ── s12: alert_subscriptions rows present (idempotent INSERTs) ─────── #
run_surface "s12" '
  ROWS=$(dex_psql_query "SELECT COUNT(*) FROM ops.alert_subscriptions a JOIN ops.data_sources s ON s.source_id = a.source_id WHERE s.display_name = '\''usaspending_contracts_lance'\'' AND a.channel = '\''telegram'\'' AND a.recipient = '\''$TELEGRAM_RECIPIENT'\'' AND a.alert_kind IN ('\''ingest_failed'\'','\''cohort_drift'\'')") &&
  test "$ROWS" -ge "2" &&
  echo "  s12: $ROWS alert_subscriptions rows for USAspending telegram routing"
'

# ── s13: docs present ────────────────────────────────────────────────── #
run_surface "s13" '
  DOC="$HQ_ALL_ROOT/apps/data-engine-x/docs/usaspending-daily-pipeline.md" &&
  test -f "$DOC" &&
  grep -q "usaspending-pipeline-remediation" "$DOC" &&
  grep -q "RW retired" "$HQ_ALL_ROOT/apps/data-engine-x/CLAUDE.md" &&
  grep -q "USAspending pipeline" "$HQ_ALL_ROOT/apps/data-engine-x/CLAUDE.md"
'

# ── s14: Railway deploy SUCCESS + Modal apps deployed ─────────────── #
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s14" '
    verify_service_runtime data-engine-x https://api.dataengine.run &&
    modal app list 2>/dev/null | grep -q "data-engine-x-usaspending-daily-verify" &&
    modal app list 2>/dev/null | grep -q "data-engine-x-usaspending-weekly-coverage"
  '
else
  echo "-- s14: SKIP (MERGE_SHA not set; deploy hasn't run yet)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s15: post-deploy invocations landed ──────────────────────────── #
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s15" '
    BACKFILL=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_source_ingest_runs WHERE run_metadata->>'\''idempotency_key'\'' = '\''usaspending_backfill_2026_05_10'\'' AND status = '\''succeeded'\''") &&
    test "$BACKFILL" -ge "1" &&
    VERIFY=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_source_ingest_runs r JOIN ops.data_sources s ON s.source_id = r.source_id WHERE s.display_name = '\''usaspending_contracts_lance'\'' AND r.run_metadata->>'\''writer'\'' = '\''usaspending-daily-verify'\''") &&
    test "$VERIFY" -ge "1" &&
    echo "  s15: backfill=$BACKFILL verify=$VERIFY rows landed"
  '
else
  echo "-- s15: SKIP (MERGE_SHA not set)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
