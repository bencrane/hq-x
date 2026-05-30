#!/usr/bin/env bash
# modal_smoke_verify.sh — post-deploy smoke test for a Modal cron app.
#
# Closes P0-4 from the 2026-05-25 systemic Modal critique
# (reports/2026-05-25-modal-setup-systemic-critique-via-mined-harness-reports.md).
#
# The audit identified that PASS_TO_PASS (pytest count) is structurally
# blind to the failure surface that just bit production: the OpenHands
# critic study showed pytest-on-benchmarks gives AUC 0.45 vs PR-merge
# signal at AUC 0.69. PR #698 was the canonical instance — 77/77 pytest
# passes; the live R2 call path crashed on first invocation.
#
# This script is the live-integration smoke gate that should run for any
# PR that touches a Modal cron. Behavior:
#
#   1. modal deploy <app_module>
#   2. modal run <app_module>::<function> --target-date=<date> --dry-run
#   3. Query bulk_ingest.feed_ingest_runs for the row we just created
#      (preferring run_id parsed from modal-run output; falling back to
#      "most recent dry_run row started after script-start")
#   4. Assert outcome == 'succeeded_dry_run' AND is_dry_run = true
#   5. PASS → exit 0, print the row's outcome/status/duration/topology.
#      FAIL → exit non-zero, dump the last 50 lines of modal-run output.
#
# This requires the orchestrator to have been migrated to the canonical
# `landing.ledger.record_run` helper (P0-2 sweep) AND the
# `bulk_ingest.feed_ingest_runs` CHECK constraint to include
# `succeeded_dry_run` (P0-1 migration). Both ship in earlier P0 PRs.
#
# USAGE
#
#   ./apps/data-engine-x/scripts/modal_smoke_verify.sh \
#       modal/usaspending_api_daily_contracts_lance_app.py \
#       run_contracts_lance_daily \
#       --target-date=2026-05-22
#
# DEFAULTS
#   target-date: 2 days ago UTC (typically has data + isn't a clean date
#                that would conflict with the scheduled cron tick).
#
# REQUIREMENTS
#   - doppler CLI with `hq-all/prd` config available
#   - modal CLI authenticated
#   - psql in PATH
#   - jq in PATH (used to parse Modal-run JSON output)
#   - cron's run function must accept --dry-run and write a ledger row
#
# EXIT CODES
#   0 — pass
#   1 — verification FAIL (outcome != succeeded_dry_run or no row found)
#   2 — usage error
#   3 — modal deploy or run command failed

set -euo pipefail

usage() {
    cat <<'USAGE' >&2
Usage: modal_smoke_verify.sh <app_module> <function> [--target-date=YYYY-MM-DD]

Arguments:
  <app_module>   path to the Modal app file (e.g. modal/foo_app.py)
  <function>     function name to invoke (e.g. run_foo_daily)
  --target-date  optional; defaults to 2 days ago UTC

Examples:
  ./modal_smoke_verify.sh modal/usaspending_api_daily_contracts_lance_app.py run_contracts_lance_daily
  ./modal_smoke_verify.sh modal/usaspending_api_daily_app.py run_api_daily_delta --target-date=2026-05-22
USAGE
    exit 2
}

if [[ $# -lt 2 ]]; then
    usage
fi

APP="$1"
FUNCTION="$2"
shift 2

# macOS / BSD `date -v` vs GNU `date -d` — handle both.
if date -u -v-2d +%Y-%m-%d >/dev/null 2>&1; then
    TARGET_DATE_DEFAULT="$(date -u -v-2d +%Y-%m-%d)"
else
    TARGET_DATE_DEFAULT="$(date -u -d '2 days ago' +%Y-%m-%d)"
fi
TARGET_DATE="$TARGET_DATE_DEFAULT"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-date=*) TARGET_DATE="${1#*=}"; shift ;;
        --target-date)   TARGET_DATE="$2"; shift 2 ;;
        -h|--help)       usage ;;
        *)               echo "Unknown arg: $1" >&2; usage ;;
    esac
done

# Resolve repo root so the script works from any cwd.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
    echo "FATAL: not in a git repo; cannot resolve apps/data-engine-x path" >&2
    exit 2
fi
DEX_DIR="$REPO_ROOT/apps/data-engine-x"
if [[ ! -d "$DEX_DIR" ]]; then
    echo "FATAL: $DEX_DIR does not exist" >&2
    exit 2
fi

# Pre-run timestamp window for the latest-row fallback (UTC, ISO8601-ish).
START_TS="$(date -u +%Y-%m-%dT%H:%M:%S)"

echo "==> Smoke target: $APP::$FUNCTION --target-date=$TARGET_DATE --dry-run"
echo "==> Pre-run timestamp: $START_TS"

# ---------------------------------------------------------------------------
# Step 0: AST ratchet gates (purely static; <2s). Closes P2-3 from the
# 2026-05-25 systemic Modal critique: any Modal app whose imports drift
# from its target script's signatures fails BEFORE we touch Modal infra.
#
# Sub-step 0a: pyflakes "undefined name" check. The AST interface test
# catches kwarg/import drift; it does NOT catch undefined locals like the
# `outcome` NameError that shipped in the P0-2 sister-migration before the
# overnight adversarial audit caught it. Pyflakes catches that class of bug
# in <100ms. We filter for `undefined name` only — pre-existing unused-
# import noise across the portfolio is out of scope for this gate.
# ---------------------------------------------------------------------------
echo "==> Step 0/4: AST ratchet gates (pyflakes + interface + retry + secret + ledger)"

# 0a — pyflakes undefined-name gate
if PYFLAKES_OUT="$( ( cd "$DEX_DIR" && python3 -m pyflakes modal/*.py 2>&1 ) )"; then
    :  # pyflakes exit 0 = nothing flagged
fi
UNDEFINED_HITS="$(echo "$PYFLAKES_OUT" | grep -E "undefined name|may be undefined" || true)"
if [[ -n "$UNDEFINED_HITS" ]]; then
    echo "FAIL: pyflakes found undefined-name errors in modal/*.py:" >&2
    echo "$UNDEFINED_HITS" >&2
    exit 1
fi
echo "    ✓ pyflakes: no undefined names"

# 0b — AST ratchets
if ! ( cd "$DEX_DIR" && python3 -m pytest \
       tests/test_modal_script_interface.py \
       tests/test_modal_retry_audit.py \
       tests/test_modal_secrets_scoped.py \
       tests/test_modal_ledger_helper.py \
       -q >/tmp/modal_smoke_ratchet.log 2>&1 ); then
    echo "FAIL: AST ratchet gates failed BEFORE deploy. Output:" >&2
    cat /tmp/modal_smoke_ratchet.log >&2
    exit 1
fi
echo "    ✓ AST ratchets clean (interface + retry + secret + ledger)"

# ---------------------------------------------------------------------------
# Step 1: modal deploy.
# ---------------------------------------------------------------------------
echo "==> Step 1/4: modal deploy $APP"
if ! ( cd "$DEX_DIR" && doppler run --project hq-all --config prd -- \
       modal deploy "$APP" >/tmp/modal_smoke_deploy.log 2>&1 ); then
    echo "FAIL: modal deploy exited non-zero. Last 30 lines:" >&2
    tail -30 /tmp/modal_smoke_deploy.log >&2
    exit 3
fi
echo "    ✓ deploy ok"

# ---------------------------------------------------------------------------
# Step 2: modal run --dry-run.
# ---------------------------------------------------------------------------
echo "==> Step 2/4: modal run $APP::$FUNCTION --dry-run"
RUN_LOG=/tmp/modal_smoke_run.log
if ! ( cd "$DEX_DIR" && doppler run --project hq-all --config prd -- \
       modal run "$APP::$FUNCTION" \
       --target-date="$TARGET_DATE" --dry-run >"$RUN_LOG" 2>&1 ); then
    echo "FAIL: modal run exited non-zero. Last 50 lines:" >&2
    tail -50 "$RUN_LOG" >&2
    exit 3
fi
echo "    ✓ run ok"

# Try to extract run_id from output. Cron functions return a dict; the
# expected shape is `{ 'run_id': '<uuid>', ... }` printed by Modal.
RUN_ID="$(grep -oE "'run_id':\s*'[a-f0-9-]{36}'" "$RUN_LOG" | head -1 \
          | sed -E "s/.*'([a-f0-9-]{36})'.*/\1/" || true)"
if [[ -n "$RUN_ID" ]]; then
    echo "    parsed run_id=$RUN_ID"
else
    echo "    (could not parse run_id from output; falling back to latest-row query)"
fi

# ---------------------------------------------------------------------------
# Step 3: query the ledger for the dry-run row.
# ---------------------------------------------------------------------------
echo "==> Step 3/4: query bulk_ingest.feed_ingest_runs"
if [[ -n "$RUN_ID" ]]; then
    QUERY="SELECT outcome, status, is_dry_run, duration_seconds, COALESCE(evidence->>'topology','') FROM bulk_ingest.feed_ingest_runs WHERE run_id = '$RUN_ID' ORDER BY started_at DESC LIMIT 1"
else
    QUERY="SELECT outcome, status, is_dry_run, duration_seconds, COALESCE(evidence->>'topology','') FROM bulk_ingest.feed_ingest_runs WHERE started_at >= '$START_TS' AND is_dry_run = TRUE ORDER BY started_at DESC LIMIT 1"
fi

ROW="$(doppler run --project hq-all --config prd -- \
       bash -c "psql -tA -F'|' \"\$DEX_DB_URL_POOLED\" -c \"$QUERY\"" 2>/dev/null \
       | head -1 || true)"

if [[ -z "$ROW" ]]; then
    echo "FAIL: no ledger row matched the smoke run" >&2
    echo "  query: $QUERY" >&2
    exit 1
fi

OUTCOME="$(echo "$ROW" | cut -d'|' -f1)"
STATUS="$(echo "$ROW" | cut -d'|' -f2)"
IS_DRY_RUN="$(echo "$ROW" | cut -d'|' -f3)"
DURATION_S="$(echo "$ROW" | cut -d'|' -f4)"
TOPOLOGY="$(echo "$ROW" | cut -d'|' -f5)"

echo "    outcome=$OUTCOME"
echo "    status=$STATUS"
echo "    is_dry_run=$IS_DRY_RUN"
echo "    duration_seconds=$DURATION_S"
echo "    topology=$TOPOLOGY"

# ---------------------------------------------------------------------------
# Step 4: assertions.
# ---------------------------------------------------------------------------
echo "==> Step 4/4: assertions"

EXIT_CODE=0
if [[ "$OUTCOME" != "succeeded_dry_run" ]]; then
    echo "    ✗ FAIL: outcome='$OUTCOME' (expected 'succeeded_dry_run' — see SECRETS.md + ledger.py)" >&2
    echo "      Common causes: app not yet migrated to canonical landing.ledger.record_run helper" >&2
    echo "                     (P0-2 sweep); legacy app still writes legacy outcome strings." >&2
    EXIT_CODE=1
fi

if [[ "$IS_DRY_RUN" != "t" && "$IS_DRY_RUN" != "true" ]]; then
    echo "    ✗ FAIL: is_dry_run='$IS_DRY_RUN' (expected 't' or 'true')" >&2
    echo "      Common cause: app's record_run() call doesn't pass RunResult.is_dry_run=True" >&2
    echo "                    in the success path; check the call site." >&2
    EXIT_CODE=1
fi

if [[ $EXIT_CODE -eq 0 ]]; then
    echo "    ✓ PASS: $APP::$FUNCTION smoke-verified"
fi

exit $EXIT_CODE
