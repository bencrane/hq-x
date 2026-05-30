#!/usr/bin/env bash
# Verification harness for /scope cycle openfda-device-pdl-bridge.
#
# Authored by the Stage 3.A audit subagent (2026-05-20 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-20-openfda-device-pdl-bridge.md
# and the validator's BLOCKING constraints (## Validator notes):
#   - PDL target: polaris-warehouse/pdl/free_companies_lance — columns
#     legal_name_normalized, state, pdl_website, pdl_id.
#   - openFDA inputs: polaris-warehouse/openfda/device_510k_lance (174,936) +
#     device_pma_lance (56,340) — applicant + state.
#   - MIN_ROWS_MATCHED = 2500 (validator-measured natural count ~5,431).
#   - Match method REUSE (L21): METHOD_NAME="company_name_state_exact"; the s1
#     generator calls register_bridge(...) ONLY and MUST NOT call
#     register_match_method_version(...) — re-registering overwrites the shared
#     method-version's input_columns and corrupts UCC×PDL / SBA / UCC×GLEIF lineage.
#
# Single-repo, single-PR cycle: every surface lands in bencrane/hq-all under
# apps/data-engine-x/. Pattern B Lance identity bridge. Pattern mirrors the
# sibling at apps/data-engine-x/scripts/migration-checks/
#   hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh
#
# Surface coverage (4 surfaces, single squash-merged PR):
#   s1   code    scripts/build_bridge_openfda_device_pdl_lance.py   (NEW, Pattern B generator)
#   s2   code    modal/openfda_device_pdl_bridge_app.py             (NEW, Modal weekly cron)
#   s3   code    app/services/lance_views.py                        (EDIT — 1 new LanceView entry)
#   s4   deploy  Modal app data-engine-x-openfda-device-pdl-bridge  (modal deploy + one modal run)
#
# Pre-deploy run: file-shape + py_compile + literal-grep gates for s1/s2/s3.
# Post-deploy run (set DEPLOYED=1): s4 deployed-state + e1 (bridge Lance
# count_rows() >= 2500 + BTREE on the join key) + reg1 (ops.bridges row +
# ops.bridge_generation_runs completed run).
#
# Usage:
#   ./openfda-device-pdl-bridge.sh                          # pre-deploy file-shape gates (s1/s2/s3)
#   ./openfda-device-pdl-bridge.sh --surface s1             # one surface
#   ./openfda-device-pdl-bridge.sh --repo hq-all            # repo filter (single repo)
#   DEPLOYED=1 ./openfda-device-pdl-bridge.sh               # incl. s4 deploy-state + e1 + reg1 gates

set -euo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# A pre-set HQ_ALL_ROOT (operator override — e.g. an isolated worktree the
# /scope executor verifies before the PR merges) wins. Otherwise fall through
# to the canonical checkout, then the legacy auxiliary clone.
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

DEX_APP="$HQ_ALL_ROOT/apps/data-engine-x"

# --- bridge constants (single source of truth for the harness) ----------- #
BRIDGE_LANCE_URI='s3://dex-raw-landing-zone/polaris-warehouse/bridges/openfda_device_pdl_lance'
MODAL_APP_NAME='data-engine-x-openfda-device-pdl-bridge'
MIN_ROWS_MATCHED=2500
BRIDGE_NAME='openfda_device_pdl'      # ops.bridges natural key (slug, no _lance suffix)
JOIN_KEY='applicant_name_normalized'  # BTREE join key on the bridge dataset

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

# --- Lance row-count floor gate (shared helper) -------------------------- #
# Usage: _lance_floor_check <lance_uri> <floor>
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
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
\"
  "
}

# --- Lance BTREE index existence check (shared helper) ------------------ #
# Usage: _lance_btree_check <lance_uri> <column_name>
_lance_btree_check() {
  local uri="$1" col="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
indices = ds.list_indices()
btree_cols = []
for idx in indices:
    fields = idx.get('fields') if isinstance(idx, dict) else getattr(idx, 'fields', [])
    itype = idx.get('type') if isinstance(idx, dict) else getattr(idx, 'index_type', '')
    if 'BTREE' in str(itype).upper() or 'BTREE' in str(idx).upper():
        for f in (fields or []):
            btree_cols.append(str(f))
if '$col' in btree_cols:
    print(f'PASS: $uri has BTREE on $col')
    sys.exit(0)
print(f'FAIL: $uri missing BTREE on $col (saw indices: {indices})')
sys.exit(1)
\"
  "
}

# ── PR-target sanity gate ──────────────────────────────────────────────── #
run_surface "p-remote" "hq-all" '
  if [[ ! -d "$HQ_ALL_ROOT/.git" ]]; then
    echo "   (no local hq-all checkout — skipping PR-target gate)"
    true
  else
    actual=$(cd "$HQ_ALL_ROOT" && git remote get-url origin)
    [[ "$actual" =~ bencrane/hq-all ]]
  fi
'

# ── s1: openFDA Medical Device × PDL bridge generator (Pattern B) ──────── #
# Validator BLOCKING constraints enforced via grep:
#   - openFDA inputs: device_510k_lance + device_pma_lance, columns applicant + state.
#   - PDL target: pdl/free_companies_lance, columns legal_name_normalized,
#     state, pdl_website, pdl_id.
#   - Output: polaris-warehouse/bridges/openfda_device_pdl_lance.
#   - Floor MIN_ROWS_MATCHED=2500 literal present; fail_bridge_run on shortfall.
#   - Match method REUSE (L21): METHOD_NAME="company_name_state_exact"; ONLY
#     register_bridge + start/complete/fail_bridge_run. register_match_method
#     and register_match_method_version MUST be ABSENT — the shared method
#     version (input_columns_left=["ORG_NAME","STATE"]) would be clobbered by
#     an UPSERT and corrupt UCC×PDL / pdl_sba_borrower / ucc_gleif lineage.
#     Precedent: build_bridge_sam_pdl_domain_lance.py:369-397.
#   - entity_name_normalize on the openFDA dedup; pdl_website carried per row.
#   - No LIST<VARCHAR> (L54).
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_openfda_device_pdl_lance.py"
  test -f "$f" &&
  # py_compile (validator: must be importable; bare python3 ok for compile-only)
  python3 -m py_compile "$f" &&
  # Output bridge dataset URI
  grep -qE "openfda_device_pdl_lance" "$f" &&
  # openFDA input datasets (510k + pma)
  grep -qE "openfda/device_510k_lance" "$f" &&
  grep -qE "openfda/device_pma_lance" "$f" &&
  # PDL target dataset
  grep -qE "pdl/free_companies_lance" "$f" &&
  # PDL exact column names the generator must read
  grep -qE "legal_name_normalized" "$f" &&
  grep -qE "pdl_website" "$f" &&
  grep -qE "pdl_id" "$f" &&
  # openFDA applicant column
  grep -qE "applicant" "$f" &&
  # Pattern B discipline: commit lock around the Lance write
  grep -qE "lance_commit_lock" "$f" &&
  # BTREE on the join key
  grep -qE "create_scalar_index" "$f" &&
  grep -qE "BTREE" "$f" &&
  # Tier model (CASE expression presence)
  grep -qE "confidence_tier" "$f" &&
  grep -qE "platinum" "$f" &&
  grep -qE "COLLISION_THRESHOLD[[:space:]]*=[[:space:]]*50" "$f" &&
  # Floor 2500 (accept 2500 / 2_500 / 2,500); HARD-FAIL gate
  grep -qE "MIN_ROWS_MATCHED[[:space:]]*=[[:space:]]*2[_,]?500" "$f" &&
  grep -qE "HARD[ _]?FAIL" "$f" &&
  # Shared entity-name normalizer
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  grep -qE "normalize_entity_name" "$f" &&
  # Registry helpers — register_bridge + run lifecycle MUST be present
  grep -qE "register_bridge" "$f" &&
  grep -qE "start_bridge_run" "$f" &&
  grep -qE "complete_bridge_run" "$f" &&
  grep -qE "fail_bridge_run" "$f" &&
  # L21 match-method REUSE: METHOD_NAME literal is company_name_state_exact
  grep -qE "company_name_state_exact" "$f" &&
  # CRITICAL L21 GREP-ASSERT: register_match_method + register_match_method_version
  # MUST be ABSENT ANYWHERE in the file (matches the UCC×PDL precedent harness,
  # which uses a bare grep — no comment exclusion). Re-registering the shared
  # company_name_state_exact version overwrites input_columns_left=["ORG_NAME","STATE"]
  # and corrupts UCC×PDL / pdl_sba_borrower / ucc_gleif provenance. Per L59 the
  # s1 generator MUST paraphrase these literals in any docstring/comment that
  # explains why they are not called (e.g. "register the per-version row").
  ! grep -qE "register_match_method_version" "$f" &&
  ! grep -qE "register_match_method\b" "$f" &&
  # L54 GREP-ASSERT: no LIST<VARCHAR> column type. The s1 generator must
  # paraphrase the literal in any comment per L59 ("pipe-delimited VARCHAR").
  ! grep -qE "LIST<VARCHAR>" "$f"
'

# ── s2: Modal app — data-engine-x-openfda-device-pdl-bridge (weekly cron) ─ #
# Directive s2 verify greps: modal.App("data-engine-x-openfda-device-pdl-bridge"),
# Cron(, delegate import of build_bridge_openfda_device_pdl_lance.
# Precedent: modal/ppp_sos_ca_bridge_app.py.
run_surface "s2" "hq-all" '
  f="$DEX_APP/modal/openfda_device_pdl_bridge_app.py"
  test -f "$f" &&
  python3 -m py_compile "$f" &&
  # Modal app name (exact)
  grep -qE "modal\.App\(\"data-engine-x-openfda-device-pdl-bridge\"\)" "$f" &&
  # Weekly Cron schedule
  grep -qE "Cron\(" "$f" &&
  # Delegates to the s1 generator
  grep -qE "build_bridge_openfda_device_pdl_lance" "$f" &&
  # Runs the generator with --apply (write path, not dry-run)
  grep -qE "\-\-apply" "$f"
'

# ── s3: lance_views.py — 1 new LanceView entry for the bridge dataset ──── #
# Append-one-entry; existing bridge entries carry name=bridges_<slug>_lance_raw,
# uri=polaris-warehouse/bridges/<slug>_lance, register_at_boot=False.
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  python3 -m py_compile "$f" &&
  # The new bridge Lance dataset literal is present
  grep -qE "openfda_device_pdl_lance" "$f" &&
  # New Lance URI for the bridge dataset
  grep -qE "polaris-warehouse/bridges/openfda_device_pdl_lance" "$f" &&
  # register_at_boot=False adjacent to the new entry (multi-thousand-row bridge;
  # per-key reads go through scanner(filter=...), not the boot Arrow bridge).
  awk "/openfda_device_pdl_lance/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s4: Modal deploy-state + e1 (bridge Lance) + reg1 (registry rows) ──── #
# Set DEPLOYED=1 AFTER `modal deploy` + the one immediate `modal run` that
# generates the bridge have both completed.
if [[ -n "${DEPLOYED:-}" ]]; then
  # s4: modal app list --json deployed-state (L62 — capitalized JSON keys).
  run_surface "s4" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      modal app list --json | jq -e \".[] | select(.Description==\\\"$MODAL_APP_NAME\\\") | select(.State==\\\"deployed\\\")\" >/dev/null
    " && echo "PASS: Modal app $MODAL_APP_NAME State=deployed"
  '

  # e1a: bridge Lance dataset count_rows() >= MIN_ROWS_MATCHED.
  run_surface "e1-floor" "hq-all" '
    _lance_floor_check "$BRIDGE_LANCE_URI" "$MIN_ROWS_MATCHED"
  '

  # e1b: BTREE on the join key.
  run_surface "e1-btree" "hq-all" '
    _lance_btree_check "$BRIDGE_LANCE_URI" "$JOIN_KEY"
  '

  # reg1a: ops.bridges has the bridge row.
  run_surface "reg1-bridge" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridges WHERE bridge_name='"'"'openfda_device_pdl'"'"' LIMIT 1")
    test "$R" = "1"
  '

  # reg1b: ops.bridge_generation_runs has a completed run for this bridge.
  run_surface "reg1-run" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridge_generation_runs WHERE bridge_name='"'"'openfda_device_pdl'"'"' AND status='"'"'completed'"'"' LIMIT 1")
    test "$R" = "1"
  '

  # reg1-guard: CRITICAL L21 — the shared company_name_state_exact v1.0.0
  # method-version row MUST NOT have been overwritten with openFDA-shape source
  # columns. The s1 generator REUSES this shared method (register_bridge only)
  # and must never call register_match_method_version — that would UPSERT the
  # version row's input_columns and corrupt the lineage of every sibling bridge
  # on this method (pdl_sba_borrower / ucc_pdl / ucc_gleif / ...).
  #
  # The robust anti-corruption assertion: the version row exists AND its
  # input_columns_left does NOT contain 'applicant' — openFDA's raw source-column
  # name, the literal that would appear ONLY if the executor wrongly re-registered
  # this version with openFDA's own input_columns. (The earlier ORG_NAME/STATE
  # literal check assumed a specific sibling-bridge's column shape; the shared
  # row's exact input_columns legitimately reflect whichever sibling bridge last
  # ran register_match_method_version — currently the SBA-borrower shape — so the
  # check must assert the *absence* of openFDA columns, not a specific sibling's
  # presence.)
  run_surface "reg1-method-not-overwritten" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.match_method_versions v JOIN ops.match_methods m USING (match_method_id) WHERE m.method_name='"'"'company_name_state_exact'"'"' AND v.semver='"'"'1.0.0'"'"' AND NOT ('"'"'applicant'"'"' = ANY(v.input_columns_left)) AND NOT ('"'"'applicant'"'"' = ANY(v.input_columns_right)) LIMIT 1")
    test "$R" = "1"
  '
else
  echo "-- s4 + e1 + reg1 gates: SKIPPED (set DEPLOYED=1 after modal deploy + modal run complete)"
  SKIP_COUNT=$((SKIP_COUNT+6))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
