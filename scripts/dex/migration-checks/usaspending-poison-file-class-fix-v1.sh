#!/usr/bin/env bash
# Verification harness for /scope cycle `usaspending-poison-file-class-fix-v1`.
#
# Authored 2026-05-13 UTC per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-13-usaspending-poison-file-class-fix-v1.md
# Refined 2026-05-13 UTC by Stage 3.A audit subagent.
#
# CANONICAL IN-REPO PATH:
#   `~/hq-all/apps/data-engine-x/scripts/migration-checks/usaspending-poison-file-class-fix-v1.sh`
#   The executor MUST copy this file into that path when opening the PR. This
#   vault-tier copy is the validator-staged + audit-refined source.
#
# Pattern: mirrors `usaspending-pipeline-remediation.sh`. Surface bodies are
# single-quoted so $VAR / $(...) defer to the doppler-injected subshell.
#
# Usage:
#   ./usaspending-poison-file-class-fix-v1.sh                  # all surfaces
#   ./usaspending-poison-file-class-fix-v1.sh --surface s5     # one surface
#   MERGE_SHA=<sha> ./usaspending-poison-file-class-fix-v1.sh  # include deploy gate
#   BASELINE_FAIL=1 ./usaspending-poison-file-class-fix-v1.sh  # invert s1/s2/s3
#                                                              # (executor proves
#                                                              # tests FAIL pre-fix)

set -euo pipefail

BASELINE_FAIL="${BASELINE_FAIL:-0}"

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

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying usaspending-poison-file-class-fix-v1 (surface=${SURFACE_FILTER:-all}, baseline_fail=${BASELINE_FAIL})"

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

# Inverted runner — s1/s2/s3 in BASELINE_FAIL=1 mode: a pytest FAIL is PASS.
run_baseline_fail_surface() {
  local id="$1" cmd="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  echo "-- $id: RUNNING (baseline_fail mode — expecting pytest FAIL)"
  if eval "$cmd"; then
    echo "-- $id: UNEXPECTED PASS (test passed on baseline — s1/s2/s3 fix already applied?)" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  else
    echo "-- $id: PASS (baseline FAILS the regression as expected)"
    PASS_COUNT=$((PASS_COUNT+1))
  fi
}

TEST_REL_PATH="tests/landing/test_r2_zero_row_safety.py"
TEST_PATH="apps/data-engine-x/$TEST_REL_PATH"

# ── s1: write_streaming{,_to_key} short-circuits on empty batches ──── #
# Verifier: pytest passes after fix; FAILS on baseline (BASELINE_FAIL=1 inverts).
# Note: cd into apps/data-engine-x so uv runs in the correct project root;
# pass the relative path from that CWD.
if [[ "$BASELINE_FAIL" == "1" ]]; then
  run_baseline_fail_surface "s1" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_write_streaming_skips_empty -q"
  '
else
  run_surface "s1" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_write_streaming_skips_empty -q"
  '
fi

# ── s2: _r2_key_exists treats 0-byte as nonexistent ─────────────────── #
if [[ "$BASELINE_FAIL" == "1" ]]; then
  run_baseline_fail_surface "s2" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_zero_byte_not_exists -q"
  '
else
  run_surface "s2" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_zero_byte_not_exists -q"
  '
fi

# ── s3: assert_landed_or_explicit_empty helper raises on bare 0 rows ── #
if [[ "$BASELINE_FAIL" == "1" ]]; then
  run_baseline_fail_surface "s3" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_assert_landed_or_explicit_empty -q"
  '
else
  run_surface "s3" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    doppler run --project hq-all --config prd -- bash -c "uv run pytest $TEST_REL_PATH::test_assert_landed_or_explicit_empty -q"
  '
fi

# ── s4: regression tests file exists, parseable, declares all three ── #
run_surface "s4" '
  TEST_FILE="$HQ_ALL_ROOT/$TEST_PATH" &&
  test -f "$TEST_FILE" &&
  python3 -c "import ast; ast.parse(open(\"$TEST_FILE\").read())" &&
  grep -q "def test_write_streaming_skips_empty" "$TEST_FILE" &&
  grep -q "def test_zero_byte_not_exists" "$TEST_FILE" &&
  grep -q "def test_assert_landed_or_explicit_empty" "$TEST_FILE"
'

# ── s5: dashboard API returns freshness_state for USAspending source ── #
# Auth: DEX-direct with super-admin API key from Doppler hq-all/prd.
# The hq-command /api/dex proxy STRIPS the client's Authorization header
# (see apps/hq-command/lib/proxy/server.ts: STRIPPED_HEADERS includes
# 'authorization') and re-injects the server-side Supabase session
# cookie's access_token. Passing HQ_COMMAND_OPERATOR_JWT via Bearer
# header therefore does NOT work through the proxy. Going DEX-direct
# (api.dataengine.run) with DEX_SERVICE_TOKEN hits the same
# router (data_source_catalog_v1.py) via require_flexible_auth, which
# accepts the API key as its first credential type.
run_surface "s5" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  DEX_URL="${DEX_API_BASE_URL:-https://api.dataengine.run}" &&
  RESP=$(doppler run --project hq-all --config prd -- bash -c "
    curl -fsS -H \"Authorization: Bearer \$DEX_SERVICE_TOKEN\" \"$DEX_URL/api/internal/data-source-catalog\"
  ") &&
  echo "$RESP" | jq -e ".data.sources[] | select(.source_slug == \"usaspending_contracts_lance\") | .freshness_state | IN(\"FRESH\",\"STALE\",\"BROKEN\")" >/dev/null &&
  echo "$RESP" | jq -e ".data.sources[] | select(.source_slug == \"usaspending_contracts_lance\") | .freshness_reason | length > 0" >/dev/null
'

# ── s6: live poison_files[] surfaces a sandbox PUT inside one refresh tick ─ #
# Per audit: poison_files is served LIVE (not from the hourly catalog cache).
# PUT a 0-byte object to the sandbox prefix (catalog slug _sandbox_dashboard_probe,
# r2_prefix _sandbox/, added by this cycle's migration), sleep 15s for R2 PUT
# consistency, assert the sandbox key appears in poison_files for that slug
# via DEX-direct call, DELETE the sandbox object.
#
# Auth: same DEX-direct + super-admin API key pattern as s5. The hq-command
# proxy strips client Authorization headers (see s5 comment).
run_surface "s6" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  SANDBOX_KEY="_sandbox/poison-probe-$(date -u +%Y%m%dT%H%M%SZ).parquet" &&
  DEX_URL="${DEX_API_BASE_URL:-https://api.dataengine.run}" &&
  doppler run --project hq-all --config prd -- bash -c "
    aws s3api put-object \
      --endpoint-url \"\$R2_ENDPOINT\" \
      --bucket dex-raw-landing-zone \
      --key \"$SANDBOX_KEY\" \
      --body /dev/null \
      >/dev/null
  " &&
  sleep 15 &&
  RESP=$(doppler run --project hq-all --config prd -- bash -c "
    curl -fsS -H \"Authorization: Bearer \$DEX_SERVICE_TOKEN\" \"$DEX_URL/api/internal/data-source-catalog\"
  ") &&
  echo "$RESP" | jq -e ".data.sources[] | select(.source_slug == \"_sandbox_dashboard_probe\") | .poison_files[] | select(. == \"$SANDBOX_KEY\")" >/dev/null &&
  doppler run --project hq-all --config prd -- bash -c "
    aws s3api delete-object \
      --endpoint-url \"\$R2_ENDPOINT\" \
      --bucket dex-raw-landing-zone \
      --key \"$SANDBOX_KEY\" \
      >/dev/null
  "
'

# ── s7: generalized verify cron writes runs for ≥2 distinct sources ─ #
# all_sources_verify_app.py runs every 15 min. After ≥1 tick it must INSERT
# rows for every active source (audit recommended insert-on-pass-and-fail).
run_surface "s7" '
  ROWS=$(dex_psql_query "SELECT COUNT(DISTINCT source_id) FROM ops.data_source_ingest_runs WHERE run_metadata->>'\''writer'\'' = '\''all-sources-verify'\'' AND completed_at >= NOW() - INTERVAL '\''2 days'\''") &&
  test "$ROWS" -ge "2"
'

# ── s8: confirmed poison files deleted from R2 ──────────────────────── #
# Fires LAST among s1-s8 (irreversible per directive `## Rollback harness`).
# Verifier scans the known partition (date=2026-05-12) plus a broader sweep
# under usaspending/ for any remaining 0-byte residue.
run_surface "s8" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  COUNT=$(doppler run --project hq-all --config prd -- bash -c "
    aws s3api list-objects-v2 \
      --endpoint-url \"\$R2_ENDPOINT\" \
      --bucket dex-raw-landing-zone \
      --prefix usaspending/contracts/api-delta/date=2026-05-12/ \
      --output json 2>/dev/null | jq \".Contents // [] | length\"
  ") &&
  test "$COUNT" = "0" &&
  REMAINING=$(doppler run --project hq-all --config prd -- bash -c "
    aws s3api list-objects-v2 \
      --endpoint-url \"\$R2_ENDPOINT\" \
      --bucket dex-raw-landing-zone \
      --prefix usaspending/ \
      --output json 2>/dev/null | jq \"[.Contents[]? | select(.Size == 0)] | length\"
  ") &&
  test "$REMAINING" = "0"
'

# ── s9: post-fix manual reruns land non-empty parquet OR explicit empty ─ #
# After s1-s8, operator manually invokes usaspending_api_daily for feed_date
# 2026-05-10, 11, 12. Each must show as succeeded with rows_ingested>0 OR a
# documented explicit-empty path. MERGE_TS = merge timestamp (ISO8601 UTC).
run_surface "s9" '
  WINDOW="${MERGE_TS:-$(date -u -v-12H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-12 hours" +%Y-%m-%dT%H:%M:%SZ)}" &&
  ROWS=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_source_ingest_runs r JOIN ops.data_sources s ON s.source_id = r.source_id WHERE s.display_name = '\''usaspending_contracts_lance'\'' AND r.completed_at >= '\''$WINDOW'\''::timestamptz AND r.status = '\''succeeded'\'' AND COALESCE((r.run_metadata->>'\''feed_date'\'')::date, NULL) IN ('\''2026-05-10'\'','\''2026-05-11'\'','\''2026-05-12'\'')") &&
  test "$ROWS" -ge "3"
'

# ── s10: deploy-verifier — both subapps healthy + dashboard reflects truth #
# Gated on MERGE_SHA env var being set (executor sets after merge).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s10" '
    # Modal apps: every usaspending-* app shows State=deployed.
    # JSON key fix: actual JQ uses .Description / .State, NOT .name / .state.
    DEPLOYED_COUNT=$(modal app list --json 2>/dev/null | jq "[.[] | select((.Description // \"\" | tostring | startswith(\"data-engine-x-usaspending\")) and (.State // \"\" | tostring) == \"deployed\")] | length") &&
    test "$DEPLOYED_COUNT" -ge "5" &&
    # HQ-command Railway latest deploy + runtime probe
    HQ_URL="${HQ_COMMAND_URL:-https://hq-command-production-8f32.up.railway.app}" &&
    verify_service_runtime hq-command "$HQ_URL" &&
    # Dashboard API returns the new fields (DEX-direct with super-admin API key;
    # proxy path will not work because hq-command strips the client Authorization
    # header — see s5/s6 comments).
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    DEX_URL="${DEX_API_BASE_URL:-https://api.dataengine.run}" &&
    RESP=$(doppler run --project hq-all --config prd -- bash -c "
      curl -fsS -H \"Authorization: Bearer \$DEX_SERVICE_TOKEN\" \"$DEX_URL/api/internal/data-source-catalog\"
    ") &&
    echo "$RESP" | jq -e ".data.sources[0] | has(\"freshness_state\") and has(\"freshness_reason\") and has(\"poison_files\")" >/dev/null
  '

  # s10-extra: novel sandbox-poison probe post-deploy — the predecessor
  # "PR claimed complete, bug refired" guard. Per audit s6 decision:
  # poison_files is served live by the DEX router (not from the hourly cache),
  # so any fresh GET sees the PUT immediately after R2 consistency settles.
  # 15s sleep covers R2 read-after-write consistency (typically <5s on Cloudflare R2).
  # Auth: DEX-direct + super-admin API key (proxy strips client Authorization).
  run_surface "s10-extra" '
    cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
    SANDBOX_KEY="_sandbox/poison-probe-$(date -u +%Y%m%dT%H%M%SZ)-postdeploy.parquet" &&
    DEX_URL="${DEX_API_BASE_URL:-https://api.dataengine.run}" &&
    doppler run --project hq-all --config prd -- bash -c "
      aws s3api put-object \
        --endpoint-url \"\$R2_ENDPOINT\" \
        --bucket dex-raw-landing-zone \
        --key \"$SANDBOX_KEY\" \
        --body /dev/null \
        >/dev/null
    " &&
    sleep 15 &&
    RESP=$(doppler run --project hq-all --config prd -- bash -c "
      curl -fsS -H \"Authorization: Bearer \$DEX_SERVICE_TOKEN\" \"$DEX_URL/api/internal/data-source-catalog\"
    ") &&
    echo "$RESP" | jq -e ".data.sources[] | select(.source_slug == \"_sandbox_dashboard_probe\") | .poison_files[] | select(. == \"$SANDBOX_KEY\")" >/dev/null &&
    echo "$RESP" | jq -e ".data.sources[] | select(.source_slug == \"_sandbox_dashboard_probe\") | .freshness_state == \"BROKEN\"" >/dev/null &&
    doppler run --project hq-all --config prd -- bash -c "
      aws s3api delete-object \
        --endpoint-url \"\$R2_ENDPOINT\" \
        --bucket dex-raw-landing-zone \
        --key \"$SANDBOX_KEY\" \
        >/dev/null
    "
  '
else
  echo "-- s10: SKIP (MERGE_SHA not set; deploy hasn't run yet)"
  echo "-- s10-extra: SKIP (MERGE_SHA not set)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
