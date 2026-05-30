#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-ca-sos-master-unload-ingest.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-ca-sos-master-unload-ingest.md
# and the validator's BLOCKING fixes (raised s5 floor to 152,358; grep-asserted
# entity_name_normalize NOT ucc_normalize on both sides; Modal hosting; L42
# ZSTD/ContentEncoding constraint; railway monitoring command).
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Pattern: mirror fmcsa-carrier-detail-lance-v1.sh + hq-all-sba-7a-essentials-lance-emit.sh.
#
# Surface coverage (9 surfaces, single PR):
#   s1   code      scripts/run_ca_sos_master_unload_to_r2.py (Modal app)
#   s2   code      scripts/run_ca_sos_entities_lance_emit.py (Modal app)
#   s3   code      scripts/run_ca_sos_principals_lance_emit.py (Modal app)
#   s4   code      scripts/run_ca_sos_agents_lance_emit.py (Modal app)
#   s5   code      scripts/build_bridge_sba_sos_ca_owner_lance.py (Modal app, Pattern B)
#   s6   migration supabase/migrations/{ts}_ca_sos_data_sources.sql
#   s7   endpoint  app/services/lance_views.py (4 new LanceView entries)
#   s8   config    Polaris Generic Tables (4 registrations)
#   s9   deploy    Railway data-engine-x auto-redeploy
#
# Usage:
#   ./hq-all-ca-sos-master-unload-ingest.sh                           # pre-deploy surfaces
#   ./hq-all-ca-sos-master-unload-ingest.sh --surface s5              # one surface
#   POPULATED=1 ./hq-all-ca-sos-master-unload-ingest.sh               # incl. Lance row-count gates
#   MERGE_SHA=<sha> ./hq-all-ca-sos-master-unload-ingest.sh           # incl. deploy verify

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
# Verifies lance.dataset(uri).count_rows() >= floor.
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
# Usage: _r2_head_object <s3_uri_no_protocol>
# Returns 0 iff the R2 object responds with HTTP 200 to a HEAD request.
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

# ── s1: R2 ZSTD Parquet landing script + 3 R2 objects exist ───────────── #
# Pattern: Modal-hosted @app.function() that:
#   - reads operator-staged zip at /Users/benjamincrane/Downloads/DataRequest...zip
#   - stream-transcodes 3 CSVs (*|* delimited, CRLF, UTF-8 with cp1252 fallback per L41)
#   - writes ZSTD Parquet to s3://dex-raw-landing-zone/sos-ca/release=2026-05-16/{entities,principals,agents}/data.parquet
#   - boto3 upload_file with ExtraArgs={'ContentType': 'application/x-parquet'} ONLY
#     (NO ContentEncoding='zstd' per L42; suffix .parquet NOT .parquet.zst)
#   - imports ONLY scripts._lib.entity_name_normalize.normalize_entity_name (NOT ucc_normalize)
#   - computes entity_name_normalized via normalize_entity_name on the upstream ENTITY_NAME
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/run_ca_sos_master_unload_to_r2.py"
  test -f "$f" &&
  # File is invoked as a Modal app (@app.function() — NOT local-Mac)
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Operator-staged zip path
  grep -qE "DataRequest0x229B7838EEF50299FDF89F08D317243BD03FFFF3" "$f" &&
  # 3 CSV file names (case-sensitive — verified by validator)
  grep -qE "Agents\.csv" "$f" &&
  grep -qE "Filings\.csv" "$f" &&
  grep -qE "Principals\.csv" "$f" &&
  # 3-char *|* delimiter
  grep -qE "\\*\\|\\*" "$f" &&
  # Output prefix per directive
  grep -qE "sos-ca/release=2026-05-16/(entities|principals|agents)" "$f" &&
  # L42 ZSTD CONSTRAINT: ContentType OK, NO ContentEncoding=zstd
  grep -qE "ContentType.*application/x-parquet" "$f" &&
  ! grep -qE "ContentEncoding.*zstd" "$f" &&
  # File suffix .parquet NOT .parquet.zst
  ! grep -qE "\.parquet\.zst" "$f" &&
  # L41 encoding: UTF-8-strict with cp1252 fallback or iconv pre-pass
  ( grep -qE "cp1252|WINDOWS-1252" "$f" || grep -qE "iconv" "$f" ) &&
  # NORMALIZER GREP-ASSERT: entity_name_normalize present, ucc_normalize +
  # normalize_party_name ABSENT (PR #459/#460 root cause; reviewer-tightened).
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  grep -qE "normalize_entity_name" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f" &&
  # POPULATED check: confirm the 3 R2 objects exist (HEAD 200) once Modal has run
  if [[ -n "${POPULATED:-}" ]]; then
    _r2_head_object "sos-ca/release=2026-05-16/entities/data.parquet" &&
    _r2_head_object "sos-ca/release=2026-05-16/principals/data.parquet" &&
    _r2_head_object "sos-ca/release=2026-05-16/agents/data.parquet"
  else
    echo "   (POPULATED unset — skipping R2 object HEAD checks for s1)"
    true
  fi
'

# ── s2: entities Lance emit (Pattern A) — Modal app ────────────────────── #
# Pattern: lance_emit.LanceEmitConfig or direct emit; BTREE on entity_num +
# entity_name_normalized; compact_files() + cleanup_old_versions(timedelta(days=7));
# wrapped in lance_commit_lock("ca_sos_entities_lance").
# Validator recommendation (non-blocking): project entity_status, standing_sos,
# suspension_date, last_si_file_date for downstream "active CA entity" filters.
run_surface "s2" "hq-all" '
  f="$DEX_APP/scripts/run_ca_sos_entities_lance_emit.py"
  test -f "$f" &&
  # Modal app shape
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Reads s1 R2 parquet
  grep -qE "sos-ca/release=2026-05-16/entities" "$f" &&
  # Writes to canonical Lance URI
  grep -qE "polaris-warehouse/sos/ca_entities_lance" "$f" &&
  # Pattern A discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "ca_sos_entities_lance" "$f" &&
  # BTREE on entity_num + entity_name_normalized
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_name_normalized.*BTREE" "$f" &&
  # Compact + cleanup
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # Validator recommendation — entity-status family projected
  grep -qE "entity_status" "$f" &&
  grep -qE "standing_sos" "$f" &&
  grep -qE "suspension_date" "$f" &&
  grep -qE "last_si_file_date" "$f"
'

# ── s3: principals Lance emit (Pattern A) — Modal app ──────────────────── #
# Same shape as s2; reads s1 principals parquet; writes ca_principals_lance.
# Per-row schema must include full_name_normalized as
#   lower(trim(concat_ws(\" \", first_name, middle_name, last_name)))
run_surface "s3" "hq-all" '
  f="$DEX_APP/scripts/run_ca_sos_principals_lance_emit.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "sos-ca/release=2026-05-16/principals" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_principals_lance" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "ca_sos_principals_lance" "$f" &&
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_name_normalized.*BTREE" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # Normalization on the entity-name spine MUST be entity_name_normalize.
  # ucc_normalize + normalize_party_name BOTH absent (reviewer-tightened).
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f" &&
  # full_name_normalized projection per directive s1 spec
  grep -qE "full_name_normalized" "$f"
'

# ── s4: agents Lance emit (Pattern A) — Modal app ─────────────────────── #
run_surface "s4" "hq-all" '
  f="$DEX_APP/scripts/run_ca_sos_agents_lance_emit.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "sos-ca/release=2026-05-16/agents" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_agents_lance" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "ca_sos_agents_lance" "$f" &&
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_name_normalized.*BTREE" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f"
'

# ── s5: SBA × SoS CA owner bridge (Pattern B) — Modal app ──────────────── #
# CRITICAL: floor = 152,358 (validator-calibrated, distinct-name overlap × 0.5).
# Tier rule: platinum=1:1 / gold=1:N|N:1 / silver=N:M≤50 / rejected>50.
# Method "legal_name_state_exact_ca" v1.0.0; bridge_version 1.0.0.
# Imports ONLY entity_name_normalize.normalize_entity_name (NOT ucc_normalize).
# Per-row provenance via match_method_registry helpers.
run_surface "s5" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_sba_sos_ca_owner_lance.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Bridge constants
  grep -qE "BRIDGE_NAME[[:space:]]*=[[:space:]]*\"sba_sos_ca_owner\"" "$f" &&
  grep -qE "METHOD_NAME[[:space:]]*=[[:space:]]*\"legal_name_state_exact_ca\"" "$f" &&
  grep -qE "(METHOD_SEMVER|METHOD_VERSION)[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "BRIDGE_VERSION[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "COLLISION_THRESHOLD[[:space:]]*=[[:space:]]*50" "$f" &&
  grep -qE "MIN_ROWS_MATCHED[[:space:]]*=[[:space:]]*152[_,]?358" "$f" &&
  # Inputs
  grep -qE "polaris-warehouse/sba/borrowers_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_principals_lance" "$f" &&
  # CA-borrowers filter
  grep -qE "borrstate.*CA" "$f" &&
  # Output
  grep -qE "polaris-warehouse/bridges/sba_sos_ca_owner_lance" "$f" &&
  # Pattern B discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "sba_sos_ca_owner_lance" "$f" &&
  grep -qE "create_scalar_index.*sba_legal_name_normalized.*BTREE" "$f" &&
  # Registry helpers (provenance)
  grep -qE "register_match_method" "$f" &&
  grep -qE "register_match_method_version" "$f" &&
  grep -qE "register_bridge" "$f" &&
  grep -qE "start_bridge_run" "$f" &&
  grep -qE "complete_bridge_run" "$f" &&
  # NORMALIZER GREP-ASSERT (CRITICAL — PR #459/#460 root cause).
  # ucc_normalize + normalize_party_name BOTH absent (reviewer-tightened).
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f"
'

# ── s6: ops.data_sources migration (4 INSERTs, ON CONFLICT DO NOTHING) ── #
# Migration filename: {YYYYMMDDHHMMSS}_ca_sos_data_sources.sql
# Uses ON CONFLICT (display_name) DO NOTHING for idempotency.
# Note: directive said source_name; actual table column is display_name (UNIQUE).
run_surface "s6" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_ca_sos_data_sources.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # All 4 display_name values present in the INSERT
  grep -qE "ca_sos_entities_lance" "$mig" &&
  grep -qE "ca_sos_principals_lance" "$mig" &&
  grep -qE "ca_sos_agents_lance" "$mig" &&
  grep -qE "sba_sos_ca_owner_lance" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO NOTHING (per directive) OR
  # DO UPDATE (per the precedent in 20260527000100_overture_sba_borrower_bridge.sql).
  # Both are idempotent; reviewer accepts either form.
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_ca_sos_data_sources\.sql$"
'

# ── s6 (POPULATED): post-apply row count ────────────────────────────── #
# After migration applies, ops.data_sources MUST contain all 4 rows.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s6-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'ca_sos_entities_lance'"'"','"'"'ca_sos_principals_lance'"'"','"'"'ca_sos_agents_lance'"'"','"'"'sba_sos_ca_owner_lance'"'"')")
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
  grep -qE "sos_ca_entities_lance_raw|ca_entities_lance_raw" "$f" &&
  grep -qE "sos_ca_principals_lance_raw|ca_principals_lance_raw" "$f" &&
  grep -qE "sos_ca_agents_lance_raw|ca_agents_lance_raw" "$f" &&
  grep -qE "bridges_sba_sos_ca_owner_lance_raw|sba_sos_ca_owner_lance_raw" "$f" &&
  # 4 new Lance URIs
  grep -qE "polaris-warehouse/sos/ca_entities_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_principals_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_agents_lance" "$f" &&
  grep -qE "polaris-warehouse/bridges/sba_sos_ca_owner_lance" "$f" &&
  # Boot ergonomics: register_at_boot=False adjacent to one of the new entries
  # (the Wave-1 datasets > a few hundred MB use this gate per Pattern A doc).
  awk "/sos.ca_(entities|principals|agents)_lance|sba_sos_ca_owner_lance/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s8: Polaris Generic Tables — 4 registrations (idempotent GET-first) ── #
# Pre-deploy file existence: confirms the 4 doppler run invocations are
# documented in the directive's executor notes OR the executor has run them.
# POPULATED: GET each via init_polaris_lance_generic.py --check-only.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s8-entities"   "hq-all" '_polaris_lance_check "sos"     "ca_entities_lance"'
  run_surface "s8-principals" "hq-all" '_polaris_lance_check "sos"     "ca_principals_lance"'
  run_surface "s8-agents"     "hq-all" '_polaris_lance_check "sos"     "ca_agents_lance"'
  run_surface "s8-bridge"     "hq-all" '_polaris_lance_check "bridges" "sba_sos_ca_owner_lance"'
else
  echo "-- s8 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET checks)"
  SKIP_COUNT=$((SKIP_COUNT+4))
fi

# ── s9: Railway data-engine-x deploy status + runtime probe ───────────── #
# Validator-fixed monitoring command:
#   railway deployment list --service data-engine-x --limit 1 --json
# This cycle does NOT add new endpoints, so only the default runtime-probe is
# needed (per apps/data-engine-x/CLAUDE.md §"Deploy verification").
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s9-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-sos-sha.$$
      actual_sha=\$(cat /tmp/dex-sos-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-sos-sha.$$
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
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance" 4500000
  '
  run_surface "btree-entities-entity-num" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance" "entity_num"
  '

  run_surface "floor-principals" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance" 6000000
  '
  run_surface "btree-principals-entity-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance" "entity_name_normalized"
  '

  run_surface "floor-agents" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance" 4000000
  '
  run_surface "btree-agents-entity-num" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance" "entity_num"
  '

  # s5 bridge: validator-calibrated 152,358 lower bound.
  run_surface "floor-bridge" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ca_owner_lance" 152358
  '
  run_surface "btree-bridge-sba-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ca_owner_lance" "sba_legal_name_normalized"
  '

  # ops.bridge_generation_runs: one completed run with rows_matched >= floor
  run_surface "bridge-ops-row" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sba_sos_ca_owner'"'"' AND status='"'"'completed'"'"' AND rows_matched >= 152358 LIMIT 1")
    test "$R" = "1"
  '
else
  echo "-- Lance row-count + BTREE + bridge-ops gates: SKIPPED (set POPULATED=1 after Modal one-shot runs)"
  SKIP_COUNT=$((SKIP_COUNT+9))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
