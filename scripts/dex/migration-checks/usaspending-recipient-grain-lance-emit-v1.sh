#!/usr/bin/env bash
# Verification harness for /scope cycle `usaspending-recipient-grain-lance-emit-v1`.
#
# Authored 2026-05-14 UTC by Stage 3.A migration auditor per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-14-usaspending-recipient-grain-lance-emit-v1.md
#
# CANONICAL IN-REPO PATH:
#   `~/hq-all/apps/data-engine-x/scripts/migration-checks/usaspending-recipient-grain-lance-emit-v1.sh`
#   The executor MUST copy this file into that path when opening the PR. This
#   vault-tier copy is the audit-staged source; reviewer may refine surface
#   bodies before executor runs against it.
#
# Pattern: mirrors `usaspending-read-path-to-r2-lance-duckdb-v1.sh`. Surface
# bodies are single-quoted so $VAR / $(...) defer to the doppler-injected
# subshell.
#
# Usage:
#   ./usaspending-recipient-grain-lance-emit-v1.sh                  # all surfaces
#   ./usaspending-recipient-grain-lance-emit-v1.sh --surface s4     # one surface
#   MERGE_SHA=<sha> ./usaspending-recipient-grain-lance-emit-v1.sh  # include post-deploy
#
# Phase 1 is the SUBSTRATE + PARITY PROOF phase. The audit auto-fire decision
# for Phase 2 (audiences.py consumer rewire) is gated on s4 passing.

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

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying usaspending-recipient-grain-lance-emit-v1 (surface=${SURFACE_FILTER:-all})"

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

# --- pinned constants per audit ----------------------------------------- #
# OPTUM PUBLIC SECTOR SOLUTIONS, INC. (validator re-queried 2026-05-14T00:52:16Z).
# These are the byte-exact parity targets s4 asserts against. Audit pinned
# these as the deterministic-parity fixture (not "current MV row at run time" —
# the harness pins the snapshot to keep s4 reproducible across cycle re-runs).
FIXTURE_UEI="XMUZGJN98231"
FIXTURE_TOTAL_OBLIGATION_365D="16074408427.68"
FIXTURE_CONTRACT_COUNT_365D="35"
FIXTURE_LATEST_CONTRACT_DATE="2026-04-01"

# MV row count + volume floor (audit math: 87,200 = 65% of 134,153).
MV_ROW_COUNT="134153"
LANCE_VOLUME_FLOOR="87200"

# Lance dataset paths — production vs sandbox separation is INVARIANT.
# s2 integration test writes to SANDBOX; s3 manual-trigger writes to PROD.
LANCE_BUCKET="dex-raw-landing-zone"
LANCE_PROD_PREFIX="polaris-warehouse/usaspending/recipient_grain_lance"
LANCE_SANDBOX_PREFIX="polaris-warehouse/usaspending/recipient_grain_lance_sandbox"
LANCE_PROD_URI="s3://${LANCE_BUCKET}/${LANCE_PROD_PREFIX}/"

# Modal app name (validator confirmed name is free).
MODAL_APP_NAME="data-engine-x-usaspending-recipient-grain-lance-emit"

# Schema column set audit pins. 15 set-aside flags (NOT 16 — directive had
# off-by-one). 14 single-col flags + 1 OR-merged JV flag = 15 total. Audit
# corrected directive Constraints in §"Audit plan" → "set_aside_agg".
SET_ASIDE_FLAGS_CSV='is_8a,is_hubzone,is_wosb,is_edwosb,is_sdvosb,is_vosb,is_sdb,is_minority_owned,is_native_american_owned,is_alaskan_native_corp,is_native_hawaiian_org,is_tribal_corp,is_nonprofit,is_educational,is_jv'

# ── s1: emit module exists + Modal app name + secrets + hybrid source pin ─ #
# Source-text checks:
#  (1) Modal app name matches the validator-confirmed free name.
#  (2) Schedule is the validator-pinned cron string.
#  (3) Both Modal secrets (dex-db, bulk-ingest-r2) are wired.
#  (4) HYBRID source pin: emit module references BOTH the typed MV (for
#      recency_agg) AND raw usaspending_contracts (for set_aside_agg). This
#      is the source-pin invariant — audit auto-fails if either source name
#      is absent because that means the emit silently chose one source and
#      drift is guaranteed on whichever surface uses the wrong source.
#  (5) action_date cast pattern `NULLIF(action_date, '')::date` (or the
#      `''::text`-annotated MV-verbatim variant) for the raw set_aside_agg
#      path (matches MV byte-for-byte per audit P5). Reviewer-broadened
#      regex tolerates: bare empty string OR `''::text` OR an outer paren
#      wrap around the NULLIF.
#  (6) Safety helper import + call (assert_landed_or_explicit_empty).
#  (7) Volume-floor pre-write guard ≥ 87,200 literal.
run_surface "s1" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  EMIT_APP="modal/usaspending_recipient_grain_lance_emit_app.py" &&
  test -f "$EMIT_APP" &&
  grep -q "data-engine-x-usaspending-recipient-grain-lance-emit" "$EMIT_APP" &&
  grep -qE "modal\.Cron\(\"0 4 \* \* \*\"\)" "$EMIT_APP" &&
  grep -q "dex-db" "$EMIT_APP" &&
  grep -q "bulk-ingest-r2" "$EMIT_APP" &&
  EMIT_SCRIPT=$(grep -oE "/root/scripts/[a-z_]+\.py" "$EMIT_APP" | head -1 | sed "s|^/root/scripts|scripts|") &&
  test -n "$EMIT_SCRIPT" &&
  test -f "$EMIT_SCRIPT" &&
  grep -q "mv_usaspending_contracts_typed" "$EMIT_SCRIPT" &&
  grep -q "FROM entities\.usaspending_contracts" "$EMIT_SCRIPT" &&
  grep -qE "\(?NULLIF\([a-z_]*\.?action_date, *'"'"''"'"'(::text)?\)\)?::date" "$EMIT_SCRIPT" &&
  grep -qE "from .*landing\.safety import|from landing\.safety import" "$EMIT_SCRIPT" &&
  grep -q "assert_landed_or_explicit_empty" "$EMIT_SCRIPT" &&
  grep -qE "(87200|87_200)" "$EMIT_SCRIPT"
'

# ── s2: unit tests pass (no integration, no R2) ────────────────────────── #
# Unit class only. Integration class is gated by @pytest.mark.integration and
# runs in s4 against the production Lance dataset.
run_surface "s2" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  doppler run --project hq-all --config prd -- bash -c "uv run pytest -q tests/test_usaspending_recipient_grain_emit.py -k '\''not integration'\''"
'

# ── s3: Modal app deployed + zero FastAPI diff (purely-additive invariant) ─ #
# Three checks composed:
#  (a) modal app list shows the deploy in "deployed" state.
#  (b) PR diff vs origin/main touches ZERO files under apps/data-engine-x/app/
#      — enforces the "PURELY ADDITIVE phase" hard constraint (zero
#      audiences.py / router / FastAPI changes). Reviewer ban-list grep.
#  (c) Sandbox vs prod path separation: integration test file references
#      "_sandbox" prefix at least once. Audit-pinned invariant — directive
#      says integration writes to a SANDBOX path, not production.
run_surface "s3" '
  modal app list --json | jq -e ".[] | select(.Name == \"$MODAL_APP_NAME\") | select(.State == \"deployed\")" >/dev/null &&
  cd "$HQ_ALL_ROOT" &&
  git fetch origin main >/dev/null 2>&1 &&
  CHANGED_APP_DIR=$(git diff --name-only origin/main...HEAD -- "apps/data-engine-x/app/" | wc -l | tr -d " ") &&
  test "$CHANGED_APP_DIR" = "0" &&
  grep -q "recipient_grain_lance_sandbox" "$HQ_ALL_ROOT/apps/data-engine-x/tests/test_usaspending_recipient_grain_emit.py"
'

# ── s4: PARITY GATE — Lance @ prod path vs MV on OPTUM UEI ──────────────── #
# This is the consequential gate. Phase 2 auto-fires IFF this passes.
# Composed checks, ALL must pass:
#  (a) Volume floor against the ACTUAL written Lance dataset: ds.count_rows()
#      >= 87,200. Since the MV is 1:1 at recipient_uei grain (validator
#      confirmed 134,153/134,153 row vs distinct count), count_rows()
#      equals distinct UEIs by construction. Reads metadata from Lance;
#      no full scan needed.
#  (b) OPTUM parity gates ALL three (via pytest test_parity_optum):
#        total_obligation_365d within ±1% of 16074408427.68
#        contract_count_365d   within ±1     of 35
#        latest_contract_date  within ±7 days of 2026-04-01
#
# Reviewer-added (2026-05-14 Stage 3.B): the previous s4 only ran
# parity_optum — a Lance dataset with 1,000 rows but correct OPTUM tuple
# would have PASSED, auto-firing Phase 2 with a tiny dataset. The
# pre-write probe in s1 source-greps the literal "87200|87_200" but never
# proves the runtime actually wrote ≥87,200 rows. Add an explicit
# runtime row-count check here so Phase 2 cannot fire on a degenerate
# Lance dataset.
#
# If parity gate fails, orchestrator fires blocker via Telegram and Phase 2
# is NOT auto-spawned. Operator returns to: Modal app deployed but consuming
# UNVERIFIED Lance dataset, production read paths unchanged.
run_surface "s4" '
  cd "$HQ_ALL_ROOT/apps/data-engine-x" &&
  doppler run --project hq-all --config prd -- bash -c "uv run python -c \"
import os, lance
ds = lance.dataset(
    '\''s3://$LANCE_BUCKET/$LANCE_PROD_PREFIX'\'',
    storage_options={
        '\''aws_endpoint'\'': os.environ['\''R2_ENDPOINT'\''],
        '\''aws_access_key_id'\'': os.environ['\''R2_ACCESS_KEY_ID'\''],
        '\''aws_secret_access_key'\'': os.environ['\''R2_SECRET_ACCESS_KEY'\''],
        '\''aws_region'\'': '\''us-east-1'\'',
        '\''aws_virtual_hosted_style_request'\'': '\''false'\'',
    },
)
rows = ds.count_rows()
print(f'\''lance_rows={rows}'\'')
assert rows >= $LANCE_VOLUME_FLOOR, f'\''volume floor breach: {rows} < $LANCE_VOLUME_FLOOR'\''
\"" &&
  doppler run --project hq-all --config prd -- bash -c "uv run pytest -q tests/test_usaspending_recipient_grain_emit.py -k '\''integration and parity_optum'\''"
'

# ── s4-supplement: standalone parity probe (operator-debug) ────────────── #
# Operator can run this surface in isolation when triaging a parity gate
# failure WITHOUT re-running the full pytest harness. Reads Lance via
# DuckDB-over-httpfs and compares against the live MV row. Tolerances
# match s4 exactly. Skip unless --surface s4-probe is explicitly passed.
if [[ "$SURFACE_FILTER" == "s4-probe" ]]; then
  run_surface "s4-probe" '
    cd "$HQ_ALL_ROOT" &&
    doppler run --project hq-all --config prd -- bash -c "uv run python -c \"
import os, duckdb, psycopg
con = duckdb.connect()
con.execute(\\\"INSTALL httpfs; LOAD httpfs;\\\")
acct = os.environ[\\\"R2_ENDPOINT\\\"].split(\\\"//\\\")[-1].split(\\\".\\\")[0]
con.execute(f\\\"CREATE SECRET (TYPE r2, KEY_ID '\''{os.environ['\''R2_ACCESS_KEY_ID'\'']}'\'', SECRET '\''{os.environ['\''R2_SECRET_ACCESS_KEY'\'']}'\'', ACCOUNT_ID '\''{acct}'\'')\\\")
import lance
ds = lance.dataset('\''s3://$LANCE_BUCKET/$LANCE_PROD_PREFIX'\'', storage_options={'\''aws_endpoint'\'': os.environ['\''R2_ENDPOINT'\''], '\''aws_access_key_id'\'': os.environ['\''R2_ACCESS_KEY_ID'\''], '\''aws_secret_access_key'\'': os.environ['\''R2_SECRET_ACCESS_KEY'\''], '\''aws_region'\'': '\''us-east-1'\'', '\''aws_virtual_hosted_style_request'\'': '\''false'\''})
lc = ds.count_rows()
print(f'\''lance_rows={lc}'\'')
assert lc >= $LANCE_VOLUME_FLOOR, f'\''volume floor breach: {lc} < $LANCE_VOLUME_FLOOR'\''
tbl = ds.to_table(filter=\\\"recipient_uei = '\''$FIXTURE_UEI'\''\\\")
assert tbl.num_rows == 1, f'\''expected exactly 1 row for $FIXTURE_UEI, got {tbl.num_rows}'\''
row = tbl.to_pylist()[0]
l_tot = float(row['\''total_obligation_365d'\''] or 0)
l_cnt = int(row['\''contract_count_365d'\''] or 0)
l_lat = str(row['\''latest_contract_date'\''])[:10]
with psycopg.connect(os.environ['\''DEX_DB_URL_DIRECT'\'']) as pg:
    r = pg.execute(\\\"SELECT total_obligation_365d, contract_count_365d, latest_contract_date FROM entities.mv_govcontracts_recipient_targeting WHERE recipient_uei = %s\\\", ('\''$FIXTURE_UEI'\'',)).fetchone()
m_tot, m_cnt, m_lat = float(r[0]), int(r[1]), str(r[2])
import datetime
dl = datetime.date.fromisoformat(l_lat); dm = datetime.date.fromisoformat(m_lat)
dollar_drift_pct = abs(l_tot - m_tot) / max(abs(m_tot), 1) * 100.0
count_drift = abs(l_cnt - m_cnt)
date_drift_days = abs((dl - dm).days)
print(f'\''lance={l_tot},{l_cnt},{l_lat} mv={m_tot},{m_cnt},{m_lat} drift_pct={dollar_drift_pct:.3f} count_drift={count_drift} date_drift={date_drift_days}d'\'')
assert dollar_drift_pct <= 1.0, f'\''dollar drift {dollar_drift_pct:.3f}% exceeds 1.0%'\''
assert count_drift <= 1, f'\''count drift {count_drift} exceeds 1'\''
assert date_drift_days <= 7, f'\''date drift {date_drift_days}d exceeds 7d'\''
print('\''PARITY OK'\'')
\""
  '
fi

# ── s5: ban-list grep — zero FastAPI / consumer-side scope creep ────────── #
# Reviewer-tier check that the PR is PURELY ADDITIVE. Fails if executor
# accidentally touched ANY file under:
#   - apps/data-engine-x/app/services/   (full subdir — directive §Out-of-scope)
#   - apps/data-engine-x/app/routers/    (full subdir — directive §Out-of-scope)
#   - apps/data-engine-x/app/main.py
# These are Phase 2's surface; Phase 1 MUST NOT touch them. Audit-mandated.
# Runs by default — not gated on MERGE_SHA, runs against the PR diff.
#
# Reviewer-widened (2026-05-14 Stage 3.B): directive §Out-of-scope bans
# the entire `app/services/*` subtree, not just `services/audiences.py`.
# The narrower audit form was a footgun — an incidental
# `services/audience_resolver.py` edit would have slipped through s5 and
# only failed via s3's broader `app/` directory check, but s3 doesn't run
# pre-deploy. Widen to match the directive verbatim.
run_surface "s5" '
  cd "$HQ_ALL_ROOT" &&
  git fetch origin main >/dev/null 2>&1 &&
  BANNED=$(git diff --name-only origin/main...HEAD -- \
    "apps/data-engine-x/app/services/" \
    "apps/data-engine-x/app/routers/" \
    "apps/data-engine-x/app/main.py" | wc -l | tr -d " ") &&
  test "$BANNED" = "0"
'

echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
