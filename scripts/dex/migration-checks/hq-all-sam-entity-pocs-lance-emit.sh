#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-sam-entity-pocs-lance-emit.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-sam-entity-pocs-lance-emit.md
# and the validator's BLOCKING fixes:
#   - Floor 1,885,939 (validator §"POC floor calibration"; 0.7 × measured 2,694,200)
#   - 6 POC kinds with EXACT column-prefix mapping (validator §"POC floor calibration"):
#       govt_bus      → govt_bus_poc_*
#       alt_govt_bus  → alt_govt_bus_poc_*
#       past_perf     → past_perf_poc_poc_*       (note double-`poc`)
#       alt_past_perf → alt_past_perf_poc_*
#       elec_bus      → elec_bus_poc_*
#       alt_elec_bus  → alt_elec_poc_bus_poc_*    (note nested-`poc_bus_poc`)
#   - Modal hosting required (validator p1): @app.function(memory=32768, timeout=10800, cpu=8)
#     MUST be launched via `modal run --detach` per FL-cycle root-cause lesson
#     (Modal CLI disconnect kills attached jobs; 10800s window matters).
#   - Normalizer: in-script `lower(trim(concat_ws(' ', first_name, middle_initial, last_name)))`
#     (DuckDB SQL OR plain Python). The _lib entity_name_normalize helper is for legal
#     entity names — MUST NOT be used here per validator §"Critical executor constraints".
#   - LANCE_BYPASS_SPILLING=true per DataFusion external-sort spill OOM lesson from CA cycle.
#   - lance_commit_lock('sam_entity_pocs_lance') wraps the write.
#   - Pattern A discipline: 1 ops.data_sources row, NO ops.bridges, NO register_match_method*.
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Mirrors hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.sh shape (direct precedent — PR #467).
#
# Surface coverage (5 surfaces, single PR):
#   s1   code      scripts/run_sam_entity_pocs_lance_emit.py (Modal app, Pattern A long-form explode)
#   s2   migration supabase/migrations/{ts}_sam_entity_pocs_data_source.sql
#   s3   endpoint  app/services/lance_views.py (1 new LanceView entry, register_at_boot=False)
#   s4   config    Polaris Generic Table registration (sam_gov.entity_pocs_lance)
#   s5   deploy    Railway data-engine-x auto-redeploy
#
# Usage:
#   ./hq-all-sam-entity-pocs-lance-emit.sh                # pre-deploy structural checks
#   ./hq-all-sam-entity-pocs-lance-emit.sh --surface s1   # single surface
#   POPULATED=1 ./hq-all-sam-entity-pocs-lance-emit.sh    # incl. Lance row-count + BTREE gates
#   MERGE_SHA=<sha> ./hq-all-sam-entity-pocs-lance-emit.sh # incl. deploy verify

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
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

# shellcheck disable=SC2034  # used inside eval'd run_surface single-quoted strings
DEX_APP="$HQ_ALL_ROOT/apps/data-engine-x"

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

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with requests python3 \"$HQ_ALL_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py\" \
      --namespace $ns --table $tbl --check-only
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

# ── s1: SAM entity-POC long-form explode → Lance emit — Modal app ─────── #
# Pattern: Modal-hosted @app.function() that:
#   - Reads sam_gov.entities_lance via PyLance scanner with PROJECTION of ONLY
#     the columns needed (UEI + 6 × 11 POC fields = ~67 cols).
#   - Explodes per POC kind (UNION ALL of 6 SELECTs in DuckDB; or 6 PyArrow slices
#     stacked) with filter: NOT (first_name IS NULL AND middle_initial IS NULL
#     AND last_name IS NULL AND title IS NULL) — at-least-one-non-null. The
#     validator's p3 cushion (0.7× multiplier) tolerates ~30% additional drop.
#   - Computes full_name_normalized via DuckDB
#       lower(trim(concat_ws(' ', first_name, middle_initial, last_name)))
#     (validator §"Normalizer" — NOT scripts._lib.entity_name_normalize.normalize_entity_name).
#   - Writes via to_arrow_reader(batch_size=100_000) inside
#     lance_commit_lock('sam_entity_pocs_lance').
#   - BTREE indices on uei + poc_kind + full_name_normalized.
#   - compact_files() + cleanup_old_versions(timedelta(days=7)).
#   - LANCE_BYPASS_SPILLING=true env var.
#   - MUST be launched via `modal run --detach` (validator p1) — grep-asserted
#     via docstring/comment reference to --detach.
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/run_sam_entity_pocs_lance_emit.py"
  test -f "$f" &&
  # Modal app shape
  grep -qE "@app\.function" "$f" &&
  grep -qE "modal\.App" "$f" &&
  # Modal memory floor: 32 GiB (validator §"Critical executor constraints"; in-memory 5.3M-row materialize + BTREE)
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  # Timeout 10800s (3h) per directive surface table
  grep -qE "timeout[[:space:]]*=[[:space:]]*10800" "$f" &&
  # Input Lance URI — sam_gov.entities_lance source
  grep -qE "polaris-warehouse/sam_gov/entities_lance" "$f" &&
  # Output Lance URI per directive
  grep -qE "polaris-warehouse/sam_gov/entity_pocs_lance" "$f" &&
  # Pattern A discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "sam_entity_pocs_lance" "$f" &&
  # 6 POC column-prefix grep-asserts (validator p2 — non-obvious spellings MUST be present)
  grep -qE "govt_bus_poc_" "$f" &&
  grep -qE "alt_govt_bus_poc_" "$f" &&
  grep -qE "past_perf_poc_poc_" "$f" &&
  grep -qE "alt_past_perf_poc_" "$f" &&
  grep -qE "elec_bus_poc_" "$f" &&
  grep -qE "alt_elec_poc_bus_poc_" "$f" &&
  # 6 POC kind literals (the discriminator column values)
  grep -qE "\"govt_bus\"|'"'"'govt_bus'"'"'" "$f" &&
  grep -qE "\"alt_govt_bus\"|'"'"'alt_govt_bus'"'"'" "$f" &&
  grep -qE "\"past_perf\"|'"'"'past_perf'"'"'" "$f" &&
  grep -qE "\"alt_past_perf\"|'"'"'alt_past_perf'"'"'" "$f" &&
  grep -qE "\"elec_bus\"|'"'"'elec_bus'"'"'" "$f" &&
  grep -qE "\"alt_elec_bus\"|'"'"'alt_elec_bus'"'"'" "$f" &&
  # BTREE on uei + poc_kind + full_name_normalized. The directive pseudocode shows
  # two acceptable patterns — explicit per-column (FL precedent) OR for-loop over
  # a tuple of column names. Accept BOTH by checking:
  #   (a) BTREE index_type is used at least once with create_scalar_index, AND
  #   (b) each column name appears somewhere in the file (column-name presence is
  #       already guaranteed by the projection grep above, but assert again for clarity).
  grep -qE "create_scalar_index.*BTREE|index_type[[:space:]]*=[[:space:]]*[\"'"'"']BTREE[\"'"'"']" "$f" &&
  ( grep -qE "create_scalar_index.*uei.*BTREE" "$f" || grep -qE "\"uei\"|'"'"'uei'"'"'" "$f" ) &&
  ( grep -qE "create_scalar_index.*poc_kind.*BTREE" "$f" || grep -qE "\"poc_kind\"|'"'"'poc_kind'"'"'" "$f" ) &&
  ( grep -qE "create_scalar_index.*full_name_normalized.*BTREE" "$f" || grep -qE "\"full_name_normalized\"|'"'"'full_name_normalized'"'"'" "$f" ) &&
  # Compact + cleanup
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # DataFusion sort-spill bypass (validator §"Commit discipline")
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  # ALL-null filter (validator p3 — at-least-one-non-null). Multi-line tolerant:
  # the directive pseudocode formats the WHERE NOT (…) conjunction across 6 lines,
  # so collapse newlines before grep.
  tr "\n" " " < "$f" | grep -qE "first_name IS NULL[[:space:]]*AND[[:space:]]*[A-Za-z_]*middle_initial IS NULL[[:space:]]*AND[[:space:]]*[A-Za-z_]*last_name IS NULL[[:space:]]*AND[[:space:]]*[A-Za-z_]*title IS NULL" &&
  # full_name_normalized normalizer (in-script lower/trim/concat_ws, NOT entity_name_normalize).
  # Multi-line tolerant: the directive pseudocode formats concat_ws across 3 lines.
  tr "\n" " " < "$f" | grep -qE "lower\(trim\(concat_ws\(.+first_name.*middle_initial.*last_name" &&
  # NORMALIZER GUARD: entity_name_normalize is for legal entity (company) names —
  # MUST NOT be used for person names here (validator §"Critical executor constraints").
  ! grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  ! grep -qE "normalize_entity_name\(" "$f" &&
  # Detach launch documented (validator p1 — Modal CLI disconnect lesson)
  grep -qE "modal run --detach" "$f" &&
  # batch_size=100_000 in to_arrow_reader call (validator s1 pseudocode)
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100_000|batch_size[[:space:]]*=[[:space:]]*100000" "$f" &&
  # POPULATED check: confirm the Lance dataset exists + row floor + BTREE indices
  if [[ -n "${POPULATED:-}" ]]; then
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance" 1885939 &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance" "uei" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance" "poc_kind" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance" "full_name_normalized"
  else
    echo "   (POPULATED unset — skipping Lance row-count + BTREE gates for s1)"
    true
  fi
'

# ── s2: ops.data_sources migration (1 INSERT, ON CONFLICT idempotent) ── #
# Migration filename: {YYYYMMDDHHMMSS}_sam_entity_pocs_data_source.sql
# Uses ON CONFLICT (display_name) DO UPDATE for idempotency
# (matches FL precedent at 20260516173956_fl_sunbiz_data_sources.sql).
run_surface "s2" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_sam_entity_pocs_data_source.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # display_name present
  grep -qE "sam_entity_pocs_lance" "$mig" &&
  # storage_uri to canonical Lance dataset path (with trailing slash per FL precedent)
  grep -qE "polaris-warehouse/sam_gov/entity_pocs_lance" "$mig" &&
  # format=lance, owner_app=data-engine-x, status=active (Pattern A discipline)
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  grep -qE "'"'"'data-engine-x'"'"'" "$mig" &&
  grep -qE "'"'"'active'"'"'" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO UPDATE (validator §"Migration policy")
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # NO ops.bridges / register_match_method* — Pattern A discipline
  ! grep -qE "ops\.bridges" "$mig" &&
  ! grep -qE "register_match_method" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_sam_entity_pocs_data_source\.sql$"
'

# ── s2 (POPULATED): post-apply row count ─────────────────────────────── #
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s2-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name='"'"'sam_entity_pocs_lance'"'"'")
    test "$COUNT" = "1"
  '
fi

# ── s3: lance_views.py — 1 new LanceView entry, register_at_boot=False ── #
# Per-key reads go through lance.dataset(uri).scanner(filter=...) directly;
# multi-million-row dataset is gated register_at_boot=False per directive.
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  grep -qE "sam_entity_pocs_lance_raw" "$f" &&
  grep -qE "polaris-warehouse/sam_gov/entity_pocs_lance" "$f" &&
  # Boot ergonomics: register_at_boot=False adjacent to the new entry
  awk "/sam_entity_pocs_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s4: Polaris Generic Table — sam_gov.entity_pocs_lance ─────────────── #
# Pre-deploy: structural-only (no Polaris write happens until post-merge).
# POPULATED: GET-only check via init_polaris_lance_generic.py --check-only.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s4" "hq-all" '_polaris_lance_check "sam_gov" "entity_pocs_lance"'
else
  echo "-- s4 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET check)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s5: Railway data-engine-x deploy status + runtime probe ───────────── #
# Validator-fixed monitoring command:
#   railway deployment list --service data-engine-x --limit 1 --json
# This cycle does NOT add new endpoints — only register_at_boot=False LanceView
# entry (boot imports it but does not materialize); default runtime probe
# is sufficient per apps/data-engine-x/CLAUDE.md §"Deploy verification".
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s5-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-sam-pocs-sha.$$
      actual_sha=\$(cat /tmp/dex-sam-pocs-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-sam-pocs-sha.$$
      [[ -n \"\$actual_sha\" ]] || { echo \"FAIL: Railway returned no SUCCESS deployment SHA\" >&2; exit 1; }
      case \"\$actual_sha\" in
        $MERGE_SHA*) exit 0 ;;
        *)
          case \"$MERGE_SHA\" in
            \$actual_sha*) exit 0 ;;
          esac
          echo \"FAIL: Railway latest .meta.commitHash=\$actual_sha != \$MERGE_SHA\" >&2
          exit 1
          ;;
      esac
    "
  '
  run_surface "s5-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s5 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
