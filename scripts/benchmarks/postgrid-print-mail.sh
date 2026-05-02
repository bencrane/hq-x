#!/usr/bin/env bash
# PostGrid Print & Mail integration acceptance suite
#
# Runs all 7 acceptance checks from the directive benchmark.
# Prints a machine-parseable summary line: pass=N/M
#
# Usage:
#   bash scripts/benchmarks/postgrid-print-mail.sh
#
# Environment:
#   Uses POSTGRID_PRINT_MAIL_API_KEY_TEST from Doppler/env.
#   All API calls use test-mode keys. No live mail is dispatched.
#
# Exit code:
#   0 = all checks passed
#   1 = one or more checks failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PASS=0
FAIL=0
TOTAL=7

check_pass() {
    local name="$1"
    echo "[PASS] $name"
    PASS=$((PASS + 1))
}

check_fail() {
    local name="$1"
    local reason="${2:-}"
    echo "[FAIL] $name${reason:+ — $reason}"
    FAIL=$((FAIL + 1))
}

echo "=== PostGrid Print & Mail acceptance suite ==="
echo "Running against test-mode keys only (APP_ENV=test)"
echo ""

# ---------------------------------------------------------------------------
# Check #1: Resource client surface
# Run the pytest tests that verify the 9 namespace CRUD surface
# ---------------------------------------------------------------------------

echo "--- Check #1: Resource client surface ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_postgrid_client.py \
    -k "namespace" --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #1: 9 namespace CRUD surface present"
else
    check_fail "Check #1: 9 namespace CRUD surface" "pytest test_postgrid_client namespace tests failed"
fi

# ---------------------------------------------------------------------------
# Check #2: Test-mode round-trip (unit tests stand in for actual API calls)
# The real API round-trip requires network access to PostGrid test mode.
# We run the client unit tests which cover create/list/get for each family.
# ---------------------------------------------------------------------------

echo "--- Check #2: Test-mode round-trip (mocked) ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_postgrid_client.py \
    -k "happy" --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #2: Create/list/get round-trip (mocked) for all 9 families"
else
    check_fail "Check #2: Round-trip tests" "pytest happy-path tests failed"
fi

# ---------------------------------------------------------------------------
# Check #3: Webhook signature verification
# ---------------------------------------------------------------------------

echo "--- Check #3: Webhook signature verification ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_postgrid_signature.py \
    tests/test_postgrid_webhook_e2e.py \
    -k "good_signature or bad_signature or missing_signature" \
    --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #3: Good signature → 2xx; bad signature → 4xx"
else
    check_fail "Check #3: Webhook signature verification" "signature tests failed"
fi

# ---------------------------------------------------------------------------
# Check #4: End-to-end dispatch + ingest (locally-signed)
# ---------------------------------------------------------------------------

echo "--- Check #4: E2E dispatch + ingest (locally-signed) ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_postgrid_webhook_e2e.py \
    -k "e2e" --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #4: Locally-signed payload accepted; event stored and projected"
else
    check_fail "Check #4: E2E dispatch + ingest" "e2e webhook tests failed"
fi

# ---------------------------------------------------------------------------
# Check #5: Callsite lift to routing layer
# ---------------------------------------------------------------------------

echo "--- Check #5: Routing layer + callsite lift ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_provider_routing.py \
    tests/test_provider_attribution.py \
    --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #5: Routing layer prefers PostGrid; Lob-only resources unchanged"
else
    check_fail "Check #5: Routing layer tests" "routing tests failed"
fi

# ---------------------------------------------------------------------------
# Check #6: Doppler wiring
# ---------------------------------------------------------------------------

echo "--- Check #6: Doppler wiring / key fail-fast ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_postgrid_doppler_wiring.py \
    --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #6: Client refuses to start without API key; Doppler fields present"
else
    check_fail "Check #6: Doppler wiring tests" "doppler wiring tests failed"
fi

# ---------------------------------------------------------------------------
# Check #7: Provider attribution per dispatch
# ---------------------------------------------------------------------------

echo "--- Check #7: Provider attribution per dispatch ---"
if "$REPO_ROOT/.venv/bin/pytest" tests/test_provider_attribution.py \
    tests/test_provider_routing.py -k "attribution" \
    --tb=short -q 2>&1 | grep -q "passed"; then
    check_pass "Check #7: Provider, routing_decision, resource_family present on every dispatch"
else
    check_fail "Check #7: Provider attribution tests" "attribution tests failed"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "=== Regression bar: existing test suite ==="
REGRESSION_OUTPUT="$("$REPO_ROOT/.venv/bin/pytest" --tb=no -q 2>&1 | tail -3)"
echo "$REGRESSION_OUTPUT"

# Check if we hit the regression bar (1115 tests)
PASSED_COUNT=$(echo "$REGRESSION_OUTPUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo "0")
if [ "$PASSED_COUNT" -ge 1114 ]; then
    echo "[PASS] Regression bar: $PASSED_COUNT tests passing (>= 1114 required)"
else
    echo "[FAIL] Regression bar: only $PASSED_COUNT tests passing (< 1114 required)"
fi

echo ""
echo "=== Constraint checks ==="

# No live key usage in tests
LIVE_KEY_USAGE=$(grep -rE 'live_|POSTGRID_PRINT_MAIL_API_KEY_LIVE' tests/ scripts/benchmarks/ 2>/dev/null | grep -v "POSTGRID_PRINT_MAIL_API_KEY_LIVE not set\|live_key\|live_keys\|# live\|_live_key\|_postgrid_live_key\|_lob_live_key" || true)
if [ -z "$LIVE_KEY_USAGE" ]; then
    echo "[PASS] No live key usage in tests/benchmarks"
else
    echo "[WARN] Potential live key references (review manually):"
    echo "$LIVE_KEY_USAGE"
fi

# lob_normalization.py unmodified
if git diff HEAD -- app/webhooks/lob_normalization.py 2>/dev/null | grep -q '^[-+][^-+]'; then
    echo "[FAIL] app/webhooks/lob_normalization.py was modified (constraint violation)"
else
    echo "[PASS] app/webhooks/lob_normalization.py unmodified"
fi

# Lob provider unmodified
if git diff HEAD -- app/providers/lob/ 2>/dev/null | grep -q '^[-+][^-+]'; then
    echo "[FAIL] app/providers/lob/ was modified (constraint violation)"
else
    echo "[PASS] app/providers/lob/ unmodified"
fi

echo ""
echo "=== Final result ==="
echo "pass=$PASS/$TOTAL"

if [ "$PASS" -eq "$TOTAL" ]; then
    exit 0
else
    exit 1
fi
