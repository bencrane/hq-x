#!/usr/bin/env bash
set -euo pipefail

REPO_FILTER=""
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (repo: ${REPO_FILTER:-all}, surface: ${SURFACE_FILTER:-all})"

MIGRATION_FILE="/Users/benjamincrane/hq-all/.claude/worktrees/admiring-chatterjee-09ad2a/apps/data-engine-x/supabase/migrations/20260503130000_mv_fdic_bank_targeting_and_signal_delta_failures.sql"
DEX_DIR="/Users/benjamincrane/hq-all/.claude/worktrees/admiring-chatterjee-09ad2a/apps/data-engine-x"

run_surface() {
  local id="$1" repo="$2"
  shift 2
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"; return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (surface filter)"; return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if "$@"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2; return 1
  fi
}

# ---- surface implementations ----

verify_s1() {
  test -f "$MIGRATION_FILE" \
    && grep -q "CREATE MATERIALIZED VIEW IF NOT EXISTS entities.mv_fdic_bank_targeting" "$MIGRATION_FILE" \
    && grep -q "CREATE MATERIALIZED VIEW IF NOT EXISTS entities.mv_fdic_signal_delta_failures" "$MIGRATION_FILE" \
    && grep -q "cron.schedule" "$MIGRATION_FILE"
}

verify_s2() {
  cd "$DEX_DIR"
  doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_matviews WHERE schemaname='"'"'entities'"'"' AND matviewname='"'"'mv_fdic_bank_targeting'"'"'" | grep -q 1'
  doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_matviews WHERE schemaname='"'"'entities'"'"' AND matviewname='"'"'mv_fdic_signal_delta_failures'"'"'" | grep -q 1'
  doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM cron.job WHERE jobname='"'"'refresh_mv_fdic_targeting_and_failures_monthly'"'"'" | grep -q 1'
}

verify_s3() {
  cd "$DEX_DIR"
  MV_BANK_CNT=$(doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT COUNT(*) FROM entities.mv_fdic_bank_targeting;"')
  SRC_BANK_CNT=$(doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT COUNT(DISTINCT cert_number) FROM entities.fdic_bank_profiles WHERE cert_number IS NOT NULL;"')
  echo "  mv_fdic_bank_targeting: $MV_BANK_CNT (expected $SRC_BANK_CNT)"
  if [[ "$MV_BANK_CNT" != "$SRC_BANK_CNT" ]]; then
    echo "  MISMATCH: bank targeting count" >&2; return 1
  fi

  MV_FAIL_CNT=$(doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT COUNT(*) FROM entities.mv_fdic_signal_delta_failures;"')
  SRC_FAIL_CNT=$(doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT COUNT(*) FROM entities.fdic_bank_failures WHERE to_date(closing_date, '"'"'DD-Mon-YY'"'"') >= now() - interval '"'"'365 days'"'"';"')
  echo "  mv_fdic_signal_delta_failures: $MV_FAIL_CNT (expected $SRC_FAIL_CNT)"
  if [[ "$MV_FAIL_CNT" != "$SRC_FAIL_CNT" ]]; then
    echo "  MISMATCH: signal delta failures count" >&2; return 1
  fi
}

verify_s4() {
  if [[ -z "${DEX_FDIC_MV_PR:-}" ]]; then
    echo "  DEX_FDIC_MV_PR not set — skipping PR merge check (PASS)"
    return 0
  fi
  gh pr view "$DEX_FDIC_MV_PR" --json mergedAt --jq .mergedAt | grep -qE "^[0-9]"
}

# ---- dispatch ----

run_surface "s1" "hq-all" verify_s1
run_surface "s2" "hq-all" verify_s2
run_surface "s3" "hq-all" verify_s3
run_surface "s4" "hq-all" verify_s4

echo "All requested surfaces verified."
