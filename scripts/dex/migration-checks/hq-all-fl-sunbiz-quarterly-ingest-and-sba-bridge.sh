#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.md
# and the validator's BLOCKING fixes:
#   - Bridge floor 242,161 (validator p176; baseline probe distinct-name overlap × 0.5)
#   - FL COR layout pinned at .../hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.py
#     (79 entity fields + 25 event fields; self-test passing; empirically cross-validated)
#   - cordata stride = 1442 bytes/line (1440 chars + CRLF); corevt stride = 664 bytes/line (662 chars + CRLF)
#   - NAICS / type_of_business columns ABSENT — DO NOT include in s1 projection (validator p153-161)
#   - Modal hosting required (validator p191): @app.function(memory=32768, timeout=14400)
#   - L42 R2 ZSTD constraint: ContentType only; NO ContentEncoding=zstd; plain .parquet suffix
#   - Normalizer: scripts._lib.entity_name_normalize ONLY; NO ucc_normalize / normalize_party_name (validator p189, PR #459/#460 reverts root cause)
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Mirrors hq-all-ca-sos-master-unload-ingest.sh shape (direct precedent — PR #464).
#
# Surface coverage (9 surfaces, single PR):
#   s1   code      scripts/run_fl_sunbiz_master_unload_to_r2.py (Modal app)
#   s2   code      scripts/run_fl_sunbiz_entities_lance_emit.py (Modal app)
#   s3   code      scripts/run_fl_sunbiz_officers_lance_emit.py (Modal app)
#   s4   code      scripts/run_fl_sunbiz_events_lance_emit.py (Modal app)
#   s5   code      scripts/build_bridge_sba_sos_fl_owner_lance.py (Modal app, Pattern B)
#   s6   migration supabase/migrations/{ts}_fl_sunbiz_data_sources.sql
#   s7   endpoint  app/services/lance_views.py (4 new LanceView entries)
#   s8   config    Polaris Generic Tables (4 registrations)
#   s9   deploy    Railway data-engine-x auto-redeploy
#
# Usage:
#   ./hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.sh                # pre-deploy surfaces
#   ./hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.sh --surface s5   # one surface
#   POPULATED=1 ./hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.sh    # incl. Lance row-count gates
#   MERGE_SHA=<sha> ./hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge.sh # incl. deploy verify

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

# --- R2 object existence check ------------------------------------------ #
# Usage: _r2_head_object <key>
_r2_head_object() {
  local key="$1"
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3api head-object --bucket dex-raw-landing-zone --key '$key' \
        --endpoint-url \$R2_ENDPOINT >/dev/null 2>&1
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

# ── p-layout: pinned FL COR layout self-test (gate before any executor work) ── #
# The cor-layout.py module is the canonical field-offset source for s1.
# Its self-test ensures all 79 COR field slices tile bytes[0..1440] and all 25
# COREVENT slices tile bytes[0..662] without gaps/overlaps. If this regresses
# the entire ingest is corrupt — gate it first.
run_surface "p-layout" "hq-all" '
  layout="$DEX_APP/scripts/migration-checks/hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.py"
  test -f "$layout" &&
  python3 "$layout" >/dev/null
'

# ── s1: R2 ZSTD Parquet landing script + 3 R2 objects exist ───────────── #
# Pattern: Modal-hosted @app.function() that:
#   - reads operator-staged zips at sos-fl/incoming/{cordata.zip,corevent.zip}
#     (staged from /Users/benjamincrane/Downloads/{cordata.zip,corevent.zip})
#   - stream-transcodes 10 cordata shards + 1 corevt file using the PINNED
#     FL COR / COR-EVENT layout (79 + 25 fixed-width fields)
#   - writes 3 ZSTD Parquet objects (entities, officers, events) to
#     s3://dex-raw-landing-zone/sos-fl/release=2026-05-16/{entities,officers,events}/data.parquet
#   - boto3 upload_file with ExtraArgs={'ContentType': 'application/x-parquet'} ONLY
#     (NO ContentEncoding='zstd' per L42; suffix .parquet NOT .parquet.zst)
#   - imports ONLY scripts._lib.entity_name_normalize.normalize_entity_name (NOT ucc_normalize)
#   - imports the pinned cor-layout module for field offsets
#   - DOES NOT fabricate naics_code / type_of_business columns (validator p153-161)
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/run_fl_sunbiz_master_unload_to_r2.py"
  test -f "$f" &&
  # File is invoked as a Modal app (@app.function() — NOT local-Mac)
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Modal memory floor: 32 GiB minimum (validator p191; 17.7 GB cordata + 9.6 GB corevent)
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  # Operator-staged zip paths (cordata + corevent)
  grep -qE "cordata\.zip" "$f" &&
  grep -qE "corevent\.zip" "$f" &&
  # Cordata shard file names referenced (10 shards or wildcard)
  ( grep -qE "cordata[0-9]\.txt" "$f" || grep -qE "cordata\*\.txt" "$f" ) &&
  grep -qE "corevt\.txt" "$f" &&
  # Pinned FL COR layout module imported (NOT a fabricated layout)
  grep -qE "cor-layout|cor_layout|COR_FIELDS|COREVENT_FIELDS" "$f" &&
  # FL line strides cited (validator p142-144 — 1442 + 664 byte/line)
  ( grep -qE "1442|COR_LINE_STRIDE_BYTES" "$f" ) &&
  ( grep -qE "664|COREVENT_LINE_STRIDE_BYTES" "$f" ) &&
  # Output prefix per directive
  grep -qE "sos-fl/release=2026-05-16/(entities|officers|events)" "$f" &&
  # L42 ZSTD CONSTRAINT: ContentType OK, NO ContentEncoding=zstd
  grep -qE "ContentType.*application/x-parquet" "$f" &&
  ! grep -qE "ContentEncoding.*zstd" "$f" &&
  # File suffix .parquet NOT .parquet.zst
  ! grep -qE "\.parquet\.zst" "$f" &&
  # NORMALIZER GREP-ASSERT: entity_name_normalize present, ucc_normalize +
  # normalize_party_name ABSENT (validator p189; PR #459/#460 root cause).
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  grep -qE "normalize_entity_name" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f" &&
  # NAICS FABRICATION GUARD (validator p5 + p153-161): these columns DO NOT
  # EXIST in the FL Sunbiz layout — s1 MUST NOT project them.
  ! grep -qE "naics_code" "$f" &&
  ! grep -qE "type_of_business" "$f" &&
  # POPULATED check: confirm the 3 R2 objects exist (HEAD 200) once Modal has run
  if [[ -n "${POPULATED:-}" ]]; then
    _r2_head_object "sos-fl/release=2026-05-16/entities/data.parquet" &&
    _r2_head_object "sos-fl/release=2026-05-16/officers/data.parquet" &&
    _r2_head_object "sos-fl/release=2026-05-16/events/data.parquet"
  else
    echo "   (POPULATED unset — skipping R2 object HEAD checks for s1)"
    true
  fi
'

# ── s2: entities Lance emit (Pattern A) — Modal app ────────────────────── #
# Reads s1 entities parquet; writes polaris-warehouse/sos/fl_entities_lance;
# BTREE on entity_num + entity_name_normalized; compact + cleanup;
# wrapped in lance_commit_lock("fl_sunbiz_entities_lance");
# LANCE_BYPASS_SPILLING=true per CA cycle DataFusion sort-spill OOM lesson.
run_surface "s2" "hq-all" '
  f="$DEX_APP/scripts/run_fl_sunbiz_entities_lance_emit.py"
  test -f "$f" &&
  # Modal app shape
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Modal memory floor: 32 GiB minimum (validator p191; multi-million-row BTREE)
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  # Reads s1 R2 parquet
  grep -qE "sos-fl/release=2026-05-16/entities" "$f" &&
  # Writes to canonical Lance URI
  grep -qE "polaris-warehouse/sos/fl_entities_lance" "$f" &&
  # Pattern A discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "fl_sunbiz_entities_lance|fl_entities_lance" "$f" &&
  # BTREE on entity_num + entity_name_normalized
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_name_normalized.*BTREE" "$f" &&
  # Compact + cleanup
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # CA cycle lesson — DataFusion sort spill bypass on multi-million-row BTREE
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  # NAICS fabrication guard (validator p5): no synthetic NAICS/type_of_business
  # in the s2 projection either — only the 79 source fields + entity_name_normalized.
  ! grep -qE "naics_code" "$f" &&
  ! grep -qE "type_of_business" "$f"
'

# ── s3: officers Lance emit (Pattern A) — Modal app ────────────────────── #
# Same shape as s2; reads s1 officers parquet; writes fl_officers_lance.
# Officer-grain: one row per (entity_num, officer_slot ∈ {1..6}) with non-blank slot.
# BTREE on entity_num + entity_name_normalized + full_name_normalized.
run_surface "s3" "hq-all" '
  f="$DEX_APP/scripts/run_fl_sunbiz_officers_lance_emit.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  grep -qE "sos-fl/release=2026-05-16/officers" "$f" &&
  grep -qE "polaris-warehouse/sos/fl_officers_lance" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "fl_sunbiz_officers_lance|fl_officers_lance" "$f" &&
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_name_normalized.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*full_name_normalized.*BTREE" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  # full_name_normalized projection per directive officer-grain spec
  grep -qE "full_name_normalized" "$f"
'

# ── s4: events Lance emit (Pattern A) — Modal app ──────────────────────── #
# Reads s1 events parquet; writes fl_events_lance; BTREE on
# event_doc_number (the entity_num join key in COREVENT) + event_effective_date.
run_surface "s4" "hq-all" '
  f="$DEX_APP/scripts/run_fl_sunbiz_events_lance_emit.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  grep -qE "sos-fl/release=2026-05-16/events" "$f" &&
  grep -qE "polaris-warehouse/sos/fl_events_lance" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "fl_sunbiz_events_lance|fl_events_lance" "$f" &&
  grep -qE "create_scalar_index.*event_doc_number.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*event_effective_date.*BTREE" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "LANCE_BYPASS_SPILLING" "$f"
'

# ── s5: SBA × SoS FL owner bridge (Pattern B) — Modal app ──────────────── #
# CRITICAL: floor = 242,161 (validator p176; distinct-name overlap × 0.5).
# Method NEW state-variant "legal_name_state_exact_fl" v1.0.0 — does NOT
# overwrite existing "legal_name_state_exact_ca" row from PR #464.
# Joins SBA FL borrowers ↔ FL SUNBIZ ENTITIES (NOT officers; directive p21).
# Tier rule: platinum=1:1 / gold=1:N|N:1 / silver=N:M≤50 / rejected>50.
# Normalizer: entity_name_normalize ONLY.
run_surface "s5" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_sba_sos_fl_owner_lance.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Modal memory floor: 32 GiB minimum (validator p191; DuckDB join on multi-million rows)
  grep -qE "memory[[:space:]]*=[[:space:]]*32768" "$f" &&
  # Bridge constants
  grep -qE "BRIDGE_NAME[[:space:]]*=[[:space:]]*\"sba_sos_fl_owner\"" "$f" &&
  grep -qE "METHOD_NAME[[:space:]]*=[[:space:]]*\"legal_name_state_exact_fl\"" "$f" &&
  grep -qE "(METHOD_SEMVER|METHOD_VERSION)[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "BRIDGE_VERSION[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "COLLISION_THRESHOLD[[:space:]]*=[[:space:]]*50" "$f" &&
  # Validator p176 floor = 242,161 (accept 242161 or 242_161)
  grep -qE "MIN_ROWS_MATCHED[[:space:]]*=[[:space:]]*242[_,]?161" "$f" &&
  # Inputs — SBA borrowers + FL ENTITIES (not officers, per directive p21)
  grep -qE "polaris-warehouse/sba/borrowers_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/fl_entities_lance" "$f" &&
  # FL-borrowers filter
  grep -qE "borrstate.*FL" "$f" &&
  # Output URI
  grep -qE "polaris-warehouse/bridges/sba_sos_fl_owner_lance" "$f" &&
  # Pattern B discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "sba_sos_fl_owner_lance" "$f" &&
  grep -qE "create_scalar_index.*sba_legal_name_normalized.*BTREE" "$f" &&
  # Registry helpers (provenance)
  grep -qE "register_match_method" "$f" &&
  grep -qE "register_match_method_version" "$f" &&
  grep -qE "register_bridge" "$f" &&
  grep -qE "start_bridge_run" "$f" &&
  grep -qE "complete_bridge_run" "$f" &&
  # NORMALIZER GREP-ASSERT (validator p1 — PR #459/#460 root cause).
  # ucc_normalize + normalize_party_name BOTH absent.
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  grep -qE "normalize_entity_name" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f"
'

# ── s6: ops.data_sources migration (4 INSERTs, ON CONFLICT idempotent) ── #
# Migration filename: {YYYYMMDDHHMMSS}_fl_sunbiz_data_sources.sql
# Uses ON CONFLICT (display_name) DO NOTHING or DO UPDATE for idempotency.
run_surface "s6" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_fl_sunbiz_data_sources.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # All 4 display_name values present in the INSERT
  grep -qE "fl_entities_lance" "$mig" &&
  grep -qE "fl_officers_lance" "$mig" &&
  grep -qE "fl_events_lance" "$mig" &&
  grep -qE "sba_sos_fl_owner_lance" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO NOTHING / DO UPDATE — both acceptable
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_fl_sunbiz_data_sources\.sql$"
'

# ── s6 (POPULATED): post-apply row count ────────────────────────────── #
# After migration applies, ops.data_sources MUST contain all 4 rows.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s6-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'fl_entities_lance'"'"','"'"'fl_officers_lance'"'"','"'"'fl_events_lance'"'"','"'"'sba_sos_fl_owner_lance'"'"')")
    test "$COUNT" = "4"
  '
fi

# ── s7: lance_views.py — 4 new LanceView entries ──────────────────────── #
# All 4 register_at_boot=False (per-key lookups use lance.dataset(uri).scanner()
# directly; the DuckDB view is for SQL-ergonomic aggregate workloads).
run_surface "s7" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  # 4 new view names
  grep -qE "fl_entities_lance_raw" "$f" &&
  grep -qE "fl_officers_lance_raw" "$f" &&
  grep -qE "fl_events_lance_raw" "$f" &&
  grep -qE "sba_sos_fl_owner_lance_raw" "$f" &&
  # 4 new Lance URIs
  grep -qE "polaris-warehouse/sos/fl_entities_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/fl_officers_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/fl_events_lance" "$f" &&
  grep -qE "polaris-warehouse/bridges/sba_sos_fl_owner_lance" "$f" &&
  # Boot ergonomics: register_at_boot=False adjacent to one of the new entries
  awk "/fl_entities_lance_raw|fl_officers_lance_raw|fl_events_lance_raw|sba_sos_fl_owner_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s8: Polaris Generic Tables — 4 registrations (idempotent GET-first) ── #
# Pre-deploy: structural-only. POPULATED: GET-only check via init_polaris_lance_generic.py --check-only.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s8-entities" "hq-all" '_polaris_lance_check "sos"     "fl_entities_lance"'
  run_surface "s8-officers" "hq-all" '_polaris_lance_check "sos"     "fl_officers_lance"'
  run_surface "s8-events"   "hq-all" '_polaris_lance_check "sos"     "fl_events_lance"'
  run_surface "s8-bridge"   "hq-all" '_polaris_lance_check "bridges" "sba_sos_fl_owner_lance"'
else
  echo "-- s8 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET checks)"
  SKIP_COUNT=$((SKIP_COUNT+4))
fi

# ── s9: Railway data-engine-x deploy status + runtime probe ───────────── #
# Validator-fixed monitoring command:
#   railway deployment list --service data-engine-x --limit 1 --json
# This cycle does NOT add new endpoints — only register_at_boot=False LanceView
# entries (boot imports them but does not materialize); default runtime probe
# is sufficient per apps/data-engine-x/CLAUDE.md §"Deploy verification".
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s9-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-fl-sos-sha.$$
      actual_sha=\$(cat /tmp/dex-fl-sos-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-fl-sos-sha.$$
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
  run_surface "s9-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s9 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# ── POPULATED block: Lance row-count floors + BTREE checks ────────────── #
# Floors from the directive's `## Volume floors` table.
# Set POPULATED=1 AFTER s2/s3/s4/s5 Modal one-shot runs have produced Lance
# datasets at the canonical URIs.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "floor-entities" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance" 10000000
  '
  run_surface "btree-entities-entity-num" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance" "entity_num"
  '
  run_surface "btree-entities-entity-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance" "entity_name_normalized"
  '

  run_surface "floor-officers" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_officers_lance" 8000000
  '
  run_surface "btree-officers-entity-num" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_officers_lance" "entity_num"
  '
  run_surface "btree-officers-full-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_officers_lance" "full_name_normalized"
  '

  run_surface "floor-events" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_events_lance" 12000000
  '
  run_surface "btree-events-doc-number" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_events_lance" "event_doc_number"
  '

  # s5 bridge: validator-calibrated 242,161 lower bound.
  run_surface "floor-bridge" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_fl_owner_lance" 242161
  '
  run_surface "btree-bridge-sba-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_fl_owner_lance" "sba_legal_name_normalized"
  '

  # ops.bridge_generation_runs: one completed run with rows_matched >= floor
  run_surface "bridge-ops-row" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sba_sos_fl_owner'"'"' AND status='"'"'completed'"'"' AND rows_matched >= 242161 LIMIT 1")
    test "$R" = "1"
  '
else
  echo "-- Lance row-count + BTREE + bridge-ops gates: SKIPPED (set POPULATED=1 after Modal one-shot runs)"
  SKIP_COUNT=$((SKIP_COUNT+11))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
