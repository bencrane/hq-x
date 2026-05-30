#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-glassdoor-substrate-v1.
#
# Refined by Stage 3.A audit subagent (2026-05-17 UTC) on top of the Stage 2
# validator scaffold (2026-05-17T02:50Z). Directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-glassdoor-substrate-v1.md
#
# Validator-stamped volume floors (from `## Validator notes`):
#   - entities.source_glassdoor_company_search     >= 15
#   - entities.source_glassdoor_company_overview   >= 8
#   - entities.source_glassdoor_company_salaries   >= 5
#   - entities.source_glassdoor_company_salaries_v2 >= 80
#   - glassdoor.<endpoint>_lance                   >= 0.9 × respective source count
#   - bridges.pdl_glassdoor_lance                  >= 5
#
# Validator-surfaced API-shape findings folded into structural assertions:
#   - /company-overview HTTP 200 on miss returns {status, request_id,
#     parameters} with NO `data` key. s2 _coerce MUST tolerate missing key.
#   - /company-salaries HTTP 200 on miss returns `data: []` (empty array);
#     on hit returns `data: {...}` (object). s2 _coerce MUST handle both.
#   - /company-search `data` is always an array (possibly empty []).
#   - /company-salaries-v2 `data` is object with embedded `salaries: [...]`
#     array; service flattens inner array onto rows-to-upsert and carries
#     parent metadata (job_title_count, num_pages, most_recent) onto each row.
#   - PDL `pdl.free_companies_lance` has BTREE on `legal_name_normalized` +
#     `pdl_id`, NOT `pdl_website`. s7 join uses DuckDB hash join after PyLance
#     scanner column-projection read.
#   - `domain_exact v1.0.0` already registered (match_method_id =
#     d35944b4-312e-4ff5-8023-05419856b917). s7 MUST NOT call
#     register_match_method_version.
#   - ops.data_sources is 5-col (no `kind` column). 5 INSERTs in 5-col shape
#     with ON CONFLICT (display_name) DO UPDATE.
#   - L42 ZSTD Parquet: ContentType='application/x-parquet' ONLY; never
#     ContentEncoding='zstd'.
#   - L47 Modal `--detach` mandatory in module docstring for all 6 Modal scripts
#     (s5 R2 snapshot, s6 × 4 Lance emits, s7 PDL bridge).
#
# Surface coverage (10 surfaces; single PR):
#   s1   migration  supabase/migrations/{ts}_glassdoor_substrate_v1.sql
#                   (4 source tables + ops.glassdoor_ingest_runs + 5 ops.data_sources rows)
#   s2   code       app/services/glassdoor_ingest.py
#                   (run_company_search/_overview/_salaries/_salaries_v2)
#   s3   code       app/routers/glassdoor_v1.py (4 POST + 1 GET) + app/main.py registration
#   s4   code       scripts/run_glassdoor_resolution_oneshot.py + ..._hydration_oneshot.py
#   s5   code       scripts/run_glassdoor_r2_snapshot.py (Modal)
#   s6   code       4 × scripts/run_glassdoor_<endpoint>_lance_emit.py (Modal Pattern A)
#   s7   code       scripts/build_bridge_pdl_glassdoor_lance.py (Modal Pattern B)
#   s8   code       app/services/lance_views.py (EDIT — append 5 LanceView entries)
#   s9   config     Polaris Generic Tables (5 registrations via init_polaris_lance_generic.py)
#   s10  deploy     Railway data-engine-x auto-redeploy + runtime probe
#
# Usage:
#   ./hq-all-glassdoor-substrate-v1.sh                          # pre-deploy structural surfaces only
#   ./hq-all-glassdoor-substrate-v1.sh --surface s5             # one surface
#   ./hq-all-glassdoor-substrate-v1.sh --repo hq-all            # filter by repo
#   POPULATED=1 ./hq-all-glassdoor-substrate-v1.sh              # incl. Postgres + R2 + Lance + Polaris populated gates
#   MERGE_SHA=<sha> ./hq-all-glassdoor-substrate-v1.sh          # incl. Railway deploy verify + runtime probe

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# Nested-worktree mode: when invoked from within a worktree (this script's
# physical path under .claude/worktrees/), the worktree is the source of truth.
# Otherwise fall back to the parent checkout.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_worktree_root="${_script_dir%/apps/data-engine-x/scripts/migration-checks}"
if [[ -f "$_worktree_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$_worktree_root/apps/data-engine-x/scripts/_lib/dex.sh"
  HQ_ALL_ROOT="$_worktree_root"
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

# ── s1: migration — 4 source tables + 1 unified ledger + 5 data_sources rows ── #
# Filename: {YYYYMMDDHHMMSS}_glassdoor_substrate_v1.sql (UTC; L3).
# Asserts:
#   - filename ^[0-9]{14}_glassdoor_substrate_v1.sql$
#   - 4 entities.source_glassdoor_<endpoint> tables present
#   - canonical 9-col provenance set (raw_source_row + 8 others)
#   - unified ops.glassdoor_ingest_runs ledger with endpoint CHECK constraint
#   - 5 ops.data_sources INSERTs in 5-col shape — NO `kind` column
#   - ON CONFLICT (display_name) DO UPDATE present
#   - IF NOT EXISTS on every CREATE TABLE
# POPULATED-gated asserts:
#   - ops.glassdoor_ingest_runs exists in information_schema
#   - 5 ops.data_sources rows present with matching display_names
#   - dex_provenance_check passes for each of the 4 source tables
run_surface "s1" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_glassdoor_substrate_v1.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_glassdoor_substrate_v1\.sql$" &&
  # 4 source tables
  grep -qE "entities\.source_glassdoor_company_search" "$mig" &&
  grep -qE "entities\.source_glassdoor_company_overview" "$mig" &&
  grep -qE "entities\.source_glassdoor_company_salaries" "$mig" &&
  grep -qE "entities\.source_glassdoor_company_salaries_v2" "$mig" &&
  # Canonical 9-col provenance — every column from CLAUDE.md §"Source ingest invariant"
  grep -qE "raw_source_row[[:space:]]+jsonb[[:space:]]+NOT NULL" "$mig" &&
  grep -qE "source_provider[[:space:]]+text[[:space:]]+NOT NULL" "$mig" &&
  grep -qE "source_filename[[:space:]]+text" "$mig" &&
  grep -qE "source_download_url[[:space:]]+text" "$mig" &&
  grep -qE "source_observed_at[[:space:]]+timestamptz" "$mig" &&
  grep -qE "source_run_metadata[[:space:]]+jsonb" "$mig" &&
  grep -qE "source_task_id[[:space:]]+text" "$mig" &&
  grep -qE "source_schedule_id[[:space:]]+text" "$mig" &&
  grep -qE "ingested_at[[:space:]]+timestamptz[[:space:]]+NOT NULL[[:space:]]+DEFAULT[[:space:]]+now\(\)" "$mig" &&
  # Unified ledger w/ endpoint discriminator (4 values)
  grep -qE "ops\.glassdoor_ingest_runs" "$mig" &&
  grep -qE "endpoint[[:space:]]+text[[:space:]]+NOT NULL" "$mig" &&
  grep -qE "company_search" "$mig" &&
  grep -qE "company_overview" "$mig" &&
  grep -qE "company_salaries" "$mig" &&
  grep -qE "company_salaries_v2" "$mig" &&
  # Status enum (per SEC ADV precedent)
  grep -qE "status[[:space:]]+text[[:space:]]+NOT NULL" "$mig" &&
  grep -qE "pending|running|completed|failed" "$mig" &&
  # Unique index on (run_id, endpoint, attempt)
  grep -qE "UNIQUE INDEX.*\(run_id, endpoint, attempt\)|\(run_id,[[:space:]]*endpoint,[[:space:]]*attempt\)" "$mig" &&
  # ops.data_sources 5-col INSERT, NO `kind` column
  grep -qE "INSERT INTO ops\.data_sources[[:space:]]*\([[:space:]]*display_name,[[:space:]]*storage_uri,[[:space:]]*format,[[:space:]]*status,[[:space:]]*owner_app" "$mig" &&
  ! grep -qE "INSERT INTO ops\.data_sources[^)]*kind" "$mig" &&
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # 5 display_names present (4 lance + 1 bridge)
  grep -qE "glassdoor_company_search_lance" "$mig" &&
  grep -qE "glassdoor_company_overview_lance" "$mig" &&
  grep -qE "glassdoor_company_salaries_lance" "$mig" &&
  grep -qE "glassdoor_company_salaries_v2_lance" "$mig" &&
  grep -qE "bridges_pdl_glassdoor_lance" "$mig" &&
  # owner_app=data-engine-x, format=lance, status=active
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  grep -qE "'"'"'active'"'"'" "$mig" &&
  grep -qE "'"'"'data-engine-x'"'"'" "$mig" &&
  # IF NOT EXISTS on every CREATE TABLE (proxy: count CREATE TABLE and
  # CREATE TABLE IF NOT EXISTS — must be equal).
  CT=$(grep -cE "^[[:space:]]*CREATE TABLE" "$mig") &&
  CTI=$(grep -cE "^[[:space:]]*CREATE TABLE IF NOT EXISTS" "$mig") &&
  test "$CT" = "$CTI" &&
  # POPULATED block
  if [[ -n "${POPULATED:-}" ]]; then
    LEDGER=$(dex_psql_query "SELECT 1 FROM information_schema.tables WHERE table_schema='"'"'ops'"'"' AND table_name='"'"'glassdoor_ingest_runs'"'"'") &&
    test "$LEDGER" = "1" &&
    DSCOUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'glassdoor_company_search_lance'"'"','"'"'glassdoor_company_overview_lance'"'"','"'"'glassdoor_company_salaries_lance'"'"','"'"'glassdoor_company_salaries_v2_lance'"'"','"'"'bridges_pdl_glassdoor_lance'"'"')") &&
    test "$DSCOUNT" = "5" &&
    dex_provenance_check "entities.source_glassdoor_company_search" &&
    dex_provenance_check "entities.source_glassdoor_company_overview" &&
    dex_provenance_check "entities.source_glassdoor_company_salaries" &&
    dex_provenance_check "entities.source_glassdoor_company_salaries_v2"
  else
    echo "   (POPULATED unset — skipping ops.* + provenance gates for s1)"
    true
  fi
'

# ── s2: app/services/glassdoor_ingest.py — 4 run_* functions ───────────── #
# Asserts:
#   - module exists; defines 4 run_* funcs
#   - uses httpx + x-api-key header from OPENWEBNINJA_API_KEY env
#   - writes to ops.glassdoor_ingest_runs (unified ledger w/ endpoint discriminator)
#   - upserts to entities.source_glassdoor_<endpoint>
#   - upserts use ON CONFLICT … WHERE … IS DISTINCT FROM EXCLUDED
#   - handles data=[]/data=dict/data-missing shape asymmetry (validator dry-pass)
run_surface "s2" "hq-all" '
  f="$DEX_APP/app/services/glassdoor_ingest.py"
  test -f "$f" &&
  # 4 run_* functions (def or `def run_<endpoint>`)
  grep -qE "^def run_company_search\(" "$f" &&
  grep -qE "^def run_company_overview\(" "$f" &&
  grep -qE "^def run_company_salaries\(" "$f" &&
  grep -qE "^def run_company_salaries_v2\(" "$f" &&
  # HTTP shape
  grep -qE "httpx" "$f" &&
  grep -qE "OPENWEBNINJA_API_KEY" "$f" &&
  grep -qE "x-api-key" "$f" &&
  # ENDPOINT_BASE points at realtime-glassdoor-data
  grep -qE "realtime-glassdoor-data" "$f" &&
  # Audit ledger writes
  grep -qE "ops\.glassdoor_ingest_runs" "$f" &&
  # Source table upserts
  grep -qE "entities\.source_glassdoor_company_search" "$f" &&
  grep -qE "entities\.source_glassdoor_company_overview" "$f" &&
  grep -qE "entities\.source_glassdoor_company_salaries" "$f" &&
  grep -qE "entities\.source_glassdoor_company_salaries_v2" "$f" &&
  # ON CONFLICT idempotency (one or more) — IS DISTINCT FROM EXCLUDED preferred
  grep -qE "ON CONFLICT" "$f" &&
  grep -qE "IS DISTINCT FROM EXCLUDED|DO UPDATE" "$f" &&
  # Shape-asymmetry handling — validator surfaced 2 distinct miss shapes:
  #   /company-overview HTTP 200 + NO `data` key on miss
  #   /company-salaries HTTP 200 + `data: []` (empty array) on miss
  # Service _coerce MUST inspect isinstance(data, dict) vs isinstance(data, list)
  # vs "data" key absent (.get returns None).
  grep -qE "isinstance\(.*data.*list\)|isinstance\(.*data.*dict\)|data_missing|data is None" "$f"
'

# ── s3: app/routers/glassdoor_v1.py — 4 POST + 1 GET, require_flexible_auth ── #
# Asserts:
#   - 4 POST /ingest/* + 1 GET /company-page route paths
#   - require_flexible_auth on ALL endpoints
#   - NO require_m2m, NO M2MContext, NO org_id/role/company_id
#   - Pydantic request models per endpoint
#   - registered in app/main.py under prefix="/api/v1/glassdoor"
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/routers/glassdoor_v1.py"
  test -f "$f" &&
  # Auth import
  grep -qE "from app\.auth import.*require_flexible_auth|require_flexible_auth.*from app\.auth" "$f" &&
  # 4 POST routes (router prefix is /api/v1/glassdoor injected in main.py;
  # route decorators use the relative path /ingest/*)
  grep -qE "@router\.post.*/ingest/company-search" "$f" &&
  grep -qE "@router\.post.*/ingest/company-overview" "$f" &&
  grep -qE "@router\.post.*/ingest/company-salaries" "$f" &&
  grep -qE "@router\.post.*/ingest/company-salaries-v2" "$f" &&
  # 1 GET route
  grep -qE "@router\.get.*/company-page" "$f" &&
  # All 5 handlers use require_flexible_auth (Depends(require_flexible_auth))
  HANDLERS=$(grep -cE "Depends\(require_flexible_auth\)" "$f") &&
  test "$HANDLERS" -ge 5 &&
  # Pydantic request models for the 4 POST endpoints
  grep -qE "class CompanySearchIngestRequest" "$f" &&
  grep -qE "class CompanyOverviewIngestRequest" "$f" &&
  grep -qE "class CompanySalariesIngestRequest" "$f" &&
  grep -qE "class CompanySalariesV2IngestRequest" "$f" &&
  # NO disallowed auth helpers
  ! grep -qE "require_m2m|M2MContext|require_system_scope|require_internal_caller|require_internal_bearer" "$f" &&
  # Service imports
  grep -qE "from app\.services\.glassdoor_ingest" "$f" &&
  # Read endpoint uses PyLance scanner for bridges/pdl_glassdoor_lance
  grep -qE "pdl_glassdoor_lance|bridges/pdl_glassdoor_lance" "$f" &&
  grep -qE "company_overview_lance" "$f" &&
  # Registered in main.py — module import + include_router with the
  # /api/v1/glassdoor prefix.
  grep -qE "from app\.routers\.glassdoor_v1 import router as glassdoor_router|from app\.routers\.glassdoor_v1 import router" "$DEX_APP/app/main.py" &&
  grep -qE "glassdoor_router|app\.routers\.glassdoor_v1" "$DEX_APP/app/main.py" &&
  grep -qE "prefix=\"/api/v1/glassdoor\"" "$DEX_APP/app/main.py"
'

# ── s4: scripts/run_glassdoor_resolution_oneshot.py + ..._hydration_oneshot.py ── #
# Resolution: 10 hardcoded names; resolution heuristic per directive.
# Hydration: 3 endpoints × resolved companies.
# Both: doppler run --project hq-all --config prd -- uv run python scripts/run_glassdoor_*.py
run_surface "s4" "hq-all" '
  f1="$DEX_APP/scripts/run_glassdoor_resolution_oneshot.py"
  f2="$DEX_APP/scripts/run_glassdoor_hydration_oneshot.py"
  test -f "$f1" -a -f "$f2" &&
  # Resolution: 10 hardcoded AE-tech-co names
  grep -qE "Salesforce" "$f1" &&
  grep -qE "HubSpot" "$f1" &&
  grep -qE "Snowflake" "$f1" &&
  grep -qE "Databricks" "$f1" &&
  grep -qE "Gong" "$f1" &&
  grep -qE "Outreach" "$f1" &&
  grep -qE "Datadog" "$f1" &&
  grep -qE "MongoDB" "$f1" &&
  grep -qE "Atlassian" "$f1" &&
  grep -qE "Zendesk" "$f1" &&
  # Both import the service module
  grep -qE "from app\.services\.glassdoor_ingest" "$f1" &&
  grep -qE "from app\.services\.glassdoor_ingest" "$f2" &&
  # Resolution heuristic stages: exact-lowercase match, PDL domain presence, salary_count × review_count tiebreak
  grep -qE "lower\(|\.lower\(\)" "$f1" &&
  grep -qE "salary_count|review_count" "$f1" &&
  # Hydration calls 3 endpoints
  grep -qE "run_company_overview" "$f2" &&
  grep -qE "run_company_salaries" "$f2" &&
  grep -qE "run_company_salaries_v2" "$f2" &&
  grep -qE "Account Executive" "$f2" &&
  # Polite rate limiting between calls
  grep -qE "time\.sleep|sleep\(" "$f2" &&
  # POPULATED gates: 4 source tables meet floor
  if [[ -n "${POPULATED:-}" ]]; then
    dex_min_row_floor_check "entities.source_glassdoor_company_search" 15 &&
    dex_min_row_floor_check "entities.source_glassdoor_company_overview" 8 &&
    dex_min_row_floor_check "entities.source_glassdoor_company_salaries" 5 &&
    dex_min_row_floor_check "entities.source_glassdoor_company_salaries_v2" 80
  else
    echo "   (POPULATED unset — skipping Postgres row-count gates for s4)"
    true
  fi
'

# ── s5: scripts/run_glassdoor_r2_snapshot.py — Modal, L42-compliant ────── #
# Asserts:
#   - Modal @app.function(memory=8192, cpu=4, timeout=3600)
#   - module docstring documents `modal run --detach` (L47)
#   - reads 4 entities.source_glassdoor_* tables
#   - writes 4 ZSTD Parquet objects to r2 with snapshot=YYYY-MM-DD partition
#   - pyarrow.ParquetWriter w/ compression="zstd", batch_size=10_000
#   - ContentType="application/x-parquet" only — NO ContentEncoding="zstd" (L42)
#   - filename .parquet NOT .parquet.zst
#   - override marker `# L42-OK-VERIFIED`
#   - extends ops.glassdoor_ingest_runs endpoint CHECK to include r2_snapshot
#     (audit decision: unify the ledger per SEC ADV precedent)
run_surface "s5" "hq-all" '
  f="$DEX_APP/scripts/run_glassdoor_r2_snapshot.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "data-engine-x-glassdoor-r2-snapshot" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*8192" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*4" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*3600" "$f" &&
  grep -qE "modal run --detach" "$f" &&
  grep -qE "@app\.local_entrypoint" "$f" &&
  # FUNCTION_SECRETS pair
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  # L42-OK-VERIFIED marker phrase
  grep -qE "L42-OK-VERIFIED" "$f" &&
  # ZSTD compression on Parquet write, batch_size=10_000
  grep -qE "compression[[:space:]]*=[[:space:]]*[\"'\''](ZSTD|zstd)[\"'\'']" "$f" &&
  grep -qE "batch_size[[:space:]]*=[[:space:]]*10[_,]?000" "$f" &&
  # L42 positive: ContentType=application/x-parquet
  grep -qE "ContentType.*application/x-parquet" "$f" &&
  # L42 negative: NO ContentEncoding=zstd
  ! grep -qE "ContentEncoding.*zstd" "$f" &&
  # filename .parquet, NOT .parquet.zst
  ! grep -qE "\.parquet\.zst" "$f" &&
  # All 4 source tables snapshotted
  grep -qE "source_glassdoor_company_search" "$f" &&
  grep -qE "source_glassdoor_company_overview" "$f" &&
  grep -qE "source_glassdoor_company_salaries" "$f" &&
  grep -qE "source_glassdoor_company_salaries_v2" "$f" &&
  # Writes audit row(s) to unified ledger
  grep -qE "ops\.glassdoor_ingest_runs" "$f" &&
  # POPULATED: confirm 4 R2 objects exist for the current UTC snapshot date
  if [[ -n "${POPULATED:-}" ]]; then
    today=$(date -u +%Y-%m-%d) &&
    _r2_head_object "glassdoor/company_search/snapshot=$today/data.parquet" &&
    _r2_head_object "glassdoor/company_overview/snapshot=$today/data.parquet" &&
    _r2_head_object "glassdoor/company_salaries/snapshot=$today/data.parquet" &&
    _r2_head_object "glassdoor/company_salaries_v2/snapshot=$today/data.parquet"
  else
    echo "   (POPULATED unset — skipping R2 head_object gates for s5)"
    true
  fi
'

# ── s6: 4 × scripts/run_glassdoor_<endpoint>_lance_emit.py — Pattern A ── #
# Shared shape (per endpoint): Modal @app.function(memory=8192, cpu=4,
# timeout=3600); reads latest snapshot Parquet via DuckDB-on-R2; writes Lance
# to s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/<endpoint>_lance/;
# lance_commit_lock + LANCE_BYPASS_SPILLING + TMPDIR=/tmp/lance +
# compact_files + cleanup_old_versions + mode="overwrite"; BTREE per directive;
# inline _normalize_domain_sql (parity with build_bridge_sam_pdl_domain_lance.py)
# + inline _normalize_entity_sql (parity with emit_pdl_free_companies_lance.py).
# Module docstring documents `modal run --detach` (L47).
# Pattern A discipline: NO register_bridge, NO register_match_method*,
# NO INSERT INTO ops.bridges.

_s6_common() {
  local f="$1"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*8192" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*4" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*3600" "$f" &&
  grep -qE "modal run --detach" "$f" &&
  grep -qE "@app\.local_entrypoint" "$f" &&
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  grep -qE "/tmp/lance" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "mode[[:space:]]*=[[:space:]]*[\"'\'']overwrite[\"'\'']" "$f" &&
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100[_,]?000" "$f" &&
  # Pattern A NEGATIVE assertions
  ! grep -qE "register_match_method" "$f" &&
  ! grep -qE "register_bridge[[:space:]]*\(" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\.bridges" "$f"
}

# s6-company-search: BTREE on glassdoor_company_id, input_query_normalized,
#                    name_normalized, website_normalized
run_surface "s6-company-search" "hq-all" '
  f="$DEX_APP/scripts/run_glassdoor_company_search_lance_emit.py"
  _s6_common "$f" &&
  grep -qE "data-engine-x-glassdoor-company-search-lance-emit" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_search_lance" "$f" &&
  grep -qE "glassdoor/company_search/snapshot=" "$f" &&
  # Inline normalizers
  grep -qE "_normalize_domain_sql" "$f" &&
  grep -qE "regexp_replace" "$f" &&
  grep -qE "_normalize_entity_sql|normalize_entity_name|entity_name_normalize" "$f" &&
  # 4 BTREE columns
  grep -qE "\"glassdoor_company_id\"|'\''glassdoor_company_id'\''" "$f" &&
  grep -qE "\"input_query_normalized\"|'\''input_query_normalized'\''" "$f" &&
  grep -qE "\"name_normalized\"|'\''name_normalized'\''" "$f" &&
  grep -qE "\"website_normalized\"|'\''website_normalized'\''" "$f"
'

# s6-company-overview: BTREE on glassdoor_company_id, name_normalized,
#                      website_normalized, industry
run_surface "s6-company-overview" "hq-all" '
  f="$DEX_APP/scripts/run_glassdoor_company_overview_lance_emit.py"
  _s6_common "$f" &&
  grep -qE "data-engine-x-glassdoor-company-overview-lance-emit" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_overview_lance" "$f" &&
  grep -qE "glassdoor/company_overview/snapshot=" "$f" &&
  grep -qE "_normalize_domain_sql" "$f" &&
  grep -qE "_normalize_entity_sql|normalize_entity_name|entity_name_normalize" "$f" &&
  grep -qE "\"glassdoor_company_id\"|'\''glassdoor_company_id'\''" "$f" &&
  grep -qE "\"name_normalized\"|'\''name_normalized'\''" "$f" &&
  grep -qE "\"website_normalized\"|'\''website_normalized'\''" "$f" &&
  grep -qE "\"industry\"|'\''industry'\''" "$f"
'

# s6-company-salaries: BTREE on glassdoor_company_id, job_title_normalized
run_surface "s6-company-salaries" "hq-all" '
  f="$DEX_APP/scripts/run_glassdoor_company_salaries_lance_emit.py"
  _s6_common "$f" &&
  grep -qE "data-engine-x-glassdoor-company-salaries-lance-emit" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_salaries_lance" "$f" &&
  grep -qE "glassdoor/company_salaries/snapshot=" "$f" &&
  grep -qE "\"glassdoor_company_id\"|'\''glassdoor_company_id'\''" "$f" &&
  grep -qE "\"job_title_normalized\"|'\''job_title_normalized'\''" "$f"
'

# s6-company-salaries-v2: BTREE on glassdoor_company_id, job_title_normalized, job_title_id
run_surface "s6-company-salaries-v2" "hq-all" '
  f="$DEX_APP/scripts/run_glassdoor_company_salaries_v2_lance_emit.py"
  _s6_common "$f" &&
  grep -qE "data-engine-x-glassdoor-company-salaries-v2-lance-emit" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_salaries_v2_lance" "$f" &&
  grep -qE "glassdoor/company_salaries_v2/snapshot=" "$f" &&
  grep -qE "\"glassdoor_company_id\"|'\''glassdoor_company_id'\''" "$f" &&
  grep -qE "\"job_title_normalized\"|'\''job_title_normalized'\''" "$f" &&
  grep -qE "\"job_title_id\"|'\''job_title_id'\''" "$f"
'

# ── s7: scripts/build_bridge_pdl_glassdoor_lance.py — Pattern B ────────── #
# Asserts:
#   - Modal @app.function(memory=49152, cpu=8, timeout=14400)
#   - module docstring documents `modal run --detach` (L47)
#   - Reuses domain_exact v1.0.0 per L21 — register_bridge + start_bridge_run +
#     complete_bridge_run only. MUST NOT call register_match_method*.
#   - PyLance scanner column-projection on PDL + Glassdoor sides (validator anti-pattern #10)
#   - Domain match primary + name match fallback; 4-tier confidence
#   - COLLISION_THRESHOLD=50
#   - BTREE on pdl_id, glassdoor_company_id, match_method
#   - bridge_run_id propagated as column on every row
#   - Pattern B Postgres write: NO ops.bridges INSERT inline (helpers do it)
run_surface "s7" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_pdl_glassdoor_lance.py"
  test -f "$f" &&
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  grep -qE "data-engine-x-pdl-glassdoor-lance" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*49152" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*8" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*14400" "$f" &&
  grep -qE "modal run --detach" "$f" &&
  grep -qE "@app\.local_entrypoint" "$f" &&
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  # L21 reuse
  grep -qE "register_bridge" "$f" &&
  grep -qE "start_bridge_run" "$f" &&
  grep -qE "complete_bridge_run" "$f" &&
  ! grep -qE "register_match_method" "$f" &&
  # Bridge identity
  grep -qE "pdl_glassdoor_lance" "$f" &&
  grep -qE "(BRIDGE_NAME|bridge_name)[[:space:]]*=[[:space:]]*[\"'\'']pdl_glassdoor_lance[\"'\'']" "$f" &&
  grep -qE "(SOURCE_LEFT|source_left)[[:space:]]*=[[:space:]]*[\"'\'']pdl_free_companies_lance[\"'\'']" "$f" &&
  grep -qE "(SOURCE_RIGHT|source_right)[[:space:]]*=[[:space:]]*[\"'\'']glassdoor_company_overview_lance[\"'\'']" "$f" &&
  grep -qE "(METHOD_NAME|method_name)[[:space:]]*=[[:space:]]*[\"'\'']domain_exact[\"'\'']" "$f" &&
  grep -qE "polaris-warehouse/bridges/pdl_glassdoor_lance" "$f" &&
  # PyLance scanner column projection on both sides (validator anti-pattern #10)
  grep -qE "lance\.dataset\(.*pdl/free_companies_lance" "$f" &&
  grep -qE "lance\.dataset\(.*glassdoor/company_overview_lance" "$f" &&
  grep -qE "\.scanner\(columns=" "$f" &&
  # Inline _normalize_domain_sql parity with s6
  grep -qE "_normalize_domain_sql" "$f" &&
  grep -qE "regexp_replace" "$f" &&
  # Domain + name fallback paths
  grep -qE "match_method" "$f" &&
  grep -qE "[\"'\'']domain[\"'\'']" "$f" &&
  grep -qE "[\"'\'']name[\"'\'']" "$f" &&
  # 4-tier confidence
  grep -qE "platinum" "$f" &&
  grep -qE "gold" "$f" &&
  grep -qE "silver" "$f" &&
  grep -qE "rejected" "$f" &&
  grep -qE "(COLLISION_THRESHOLD|collision_threshold)[[:space:]]*=[[:space:]]*50" "$f" &&
  # Lance discipline
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  grep -qE "/tmp/lance" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "mode[[:space:]]*=[[:space:]]*[\"'\'']overwrite[\"'\'']" "$f" &&
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100[_,]?000" "$f" &&
  # 3 BTREE columns
  grep -qE "\"pdl_id\"|'\''pdl_id'\''" "$f" &&
  grep -qE "\"glassdoor_company_id\"|'\''glassdoor_company_id'\''" "$f" &&
  grep -qE "\"match_method\"|'\''match_method'\''" "$f" &&
  # bridge_run_id propagated
  grep -qE "bridge_run_id" "$f" &&
  # Pattern B negative — no direct ops.bridges INSERTs (helpers do it)
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\.bridges" "$f"
'

# ── s8: app/services/lance_views.py — 5 new LanceView entries ─────────── #
# All register_at_boot=False per directive § s8.
run_surface "s8" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  # 5 view names
  grep -qE "glassdoor_company_search_lance_raw" "$f" &&
  grep -qE "glassdoor_company_overview_lance_raw" "$f" &&
  grep -qE "glassdoor_company_salaries_lance_raw" "$f" &&
  grep -qE "glassdoor_company_salaries_v2_lance_raw" "$f" &&
  grep -qE "bridges_pdl_glassdoor_lance_raw" "$f" &&
  # 5 URIs
  grep -qE "polaris-warehouse/glassdoor/company_search_lance" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_overview_lance" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_salaries_lance" "$f" &&
  grep -qE "polaris-warehouse/glassdoor/company_salaries_v2_lance" "$f" &&
  grep -qE "polaris-warehouse/bridges/pdl_glassdoor_lance" "$f" &&
  # Each new entry carries register_at_boot=False (per-entry awk slice)
  awk "/glassdoor_company_search_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  awk "/glassdoor_company_overview_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  awk "/glassdoor_company_salaries_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  awk "/glassdoor_company_salaries_v2_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  awk "/bridges_pdl_glassdoor_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s9: Polaris Generic Tables — 5 registrations (POPULATED only) ──────── #
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s9-company-search"     "hq-all" '_polaris_lance_check "glassdoor" "company_search_lance"'
  run_surface "s9-company-overview"   "hq-all" '_polaris_lance_check "glassdoor" "company_overview_lance"'
  run_surface "s9-company-salaries"   "hq-all" '_polaris_lance_check "glassdoor" "company_salaries_lance"'
  run_surface "s9-company-salariesv2" "hq-all" '_polaris_lance_check "glassdoor" "company_salaries_v2_lance"'
  run_surface "s9-bridge"             "hq-all" '_polaris_lance_check "bridges"   "pdl_glassdoor_lance"'
else
  echo "-- s9 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET checks × 5)"
  SKIP_COUNT=$((SKIP_COUNT+5))
fi

# ── s10: Railway deploy status + runtime probe ───────────────────────── #
# Default probe `/api/v1/internal/observability/sources` exercises the full
# FastAPI request-handler import graph (catches new-router import errors per
# CLAUDE.md §"Deploy verification"). New endpoints not added to runtime probes
# this cycle (audit decision: default probe is sufficient — if glassdoor_v1
# has any import error, the default probe will 502).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s10-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-glassdoor-sha.\$\$
      actual_sha=\$(cat /tmp/dex-glassdoor-sha.\$\$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-glassdoor-sha.\$\$
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
  run_surface "s10-runtime-probe" "hq-all" '
    # shellcheck source=/dev/null
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s10 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# ── POPULATED block: Lance row-count floors + BTREE checks ────────────── #
# Floors per directive Refined volume floors table (replaces the directive's
# initial proposed floors).
# Set POPULATED=1 AFTER:
#   s4 one-shots → entities.source_glassdoor_*  rows present
#   s5 R2 snapshot → 4 R2 parquets present
#   s6 × 4 Lance emits → 4 Lance datasets present
#   s7 PDL bridge → bridges.pdl_glassdoor_lance present + ops.bridges row written
#   s9 Polaris → 5 Generic Tables registered
if [[ -n "${POPULATED:-}" ]]; then
  # Lance row-count floors (0.9 × source count) — calibrated against floors stamped above.
  # ceil(0.9 × 15) = 14; ceil(0.9 × 8) = 8; ceil(0.9 × 5) = 5; ceil(0.9 × 80) = 72.
  run_surface "floor-lance-search" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_search_lance" 14
  '
  run_surface "floor-lance-overview" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance" 8
  '
  run_surface "floor-lance-salaries" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_lance" 5
  '
  run_surface "floor-lance-salaries-v2" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance" 72
  '

  # BTREE checks — universal glassdoor_company_id + per-endpoint declared columns
  run_surface "btree-search-cid" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_search_lance" "glassdoor_company_id"
  '
  run_surface "btree-overview-cid" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance" "glassdoor_company_id"
  '
  run_surface "btree-overview-website" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance" "website_normalized"
  '
  run_surface "btree-salaries-cid" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_lance" "glassdoor_company_id"
  '
  run_surface "btree-salariesv2-cid" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance" "glassdoor_company_id"
  '
  run_surface "btree-salariesv2-jtid" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance" "job_title_id"
  '

  # Bridge floor + BTREE
  run_surface "floor-bridge" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance" 5
  '
  run_surface "btree-bridge-pdl" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance" "pdl_id"
  '
  run_surface "btree-bridge-gd" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance" "glassdoor_company_id"
  '
  run_surface "btree-bridge-method" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance" "match_method"
  '

  # ops.bridges row present for pdl_glassdoor_lance
  run_surface "ops-bridges-row" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridges WHERE bridge_name='"'"'pdl_glassdoor_lance'"'"' LIMIT 1")
    test "$R" = "1"
  '

  # ops.bridge_generation_runs completed run with rows_matched >= 5
  # (schema: rows_matched is a top-level int column, not jsonb metrics → ->>)
  run_surface "ops-bridge-run-completed" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridge_generation_runs r JOIN ops.bridges b ON b.bridge_id = r.bridge_id WHERE b.bridge_name='"'"'pdl_glassdoor_lance'"'"' AND r.status='"'"'completed'"'"' AND COALESCE(r.rows_matched, 0) >= 5 LIMIT 1")
    test "$R" = "1"
  '

  # L21 invariant: domain_exact v1.0.0 unchanged. method_method_id MUST equal the
  # validator-confirmed UUID. Any drift means s7 corrupted the shared row.
  run_surface "l21-domain-exact-unchanged" "hq-all" '
    R=$(dex_psql_query "SELECT match_method_id FROM ops.match_methods WHERE method_name='"'"'domain_exact'"'"' LIMIT 1")
    test "$R" = "d35944b4-312e-4ff5-8023-05419856b917"
  '
else
  echo "-- POPULATED gates (Lance floors + BTREE + ops.bridges + L21 invariant): SKIPPED (set POPULATED=1)"
  SKIP_COUNT=$((SKIP_COUNT+16))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
