#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-usaspending-db-dump-multi-table-lance-ingest.
#
# Frozen by validator (2026-05-17 UTC) from directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-17-hq-all-usaspending-db-dump-multi-table-lance-ingest.md
#
# Q4 RESOLVED by audit (2026-05-17 UTC):
#   Discovery API:  GET https://api.usaspending.gov/api/v2/bulk_download/list_database_download_files/
#   Response shape: {"full_download_file": {"file_name": "...", "url": "..."}, ...}
#   URL pattern:    https://files.usaspending.gov/database_download/usaspending-db_YYYYMMDD.zip
#                   (ZIP wrapper — NOT raw .dump as the directive originally assumed)
#   Inner shape:    `pg_dump -Fd` (DIRECTORY format) — 73 numbered <n>.dat.gz data files
#                   + 1 toc.dat table-of-contents file, ZIP64-packaged.
#   Size:           ~172 GB compressed (file ~5 GB+; uncompressed ~3x).
#   Cadence:        monthly (latest at audit time: usaspending-db_20260506.zip, dated 2026-05-07).
#   Implication:    pg_restore must be called with the EXTRACTED DIRECTORY path,
#                   not a single .dump file: `pg_restore --data-only -t <table> -f - /cache/usaspending_db/`
#
# REQUIRES_AUDIT residual: per-table PG-type -> Arrow-type mapping is ground-truthed
# by the EXECUTOR at cycle start via `pg_restore -l /cache/usaspending_db/` (TOC dump);
# validator-frozen TABLE_PK + TABLE_FLOOR remain authoritative absent upstream schema rename.
#
# Pattern A pull-through x 12 tables from `pg_dump -Fd` directory archive ->
# pg_restore --data-only -f - stdout -> COPY-text parser -> pyarrow RecordBatch
# -> lance.write_dataset(...) directly. No intermediate R2 Parquet.
#
# Decomposition note: directive validator flagged DECOMPOSITION-CHECK: REQUIRED
# (surfaces=8, repos=1). Stage 2.5 will judge KEEP-vs-SPLIT. If SPLIT, this
# harness's per-table loops must be sub-selected to the surfaces in each child
# cycle's directive.
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Mirrors hq-all-usaspending-subawards-lance-emit.sh shape (precedent PR #471).
#
# Surface coverage (8 surfaces):
#   s1  code      modal/usaspending_db_dump_to_r2_and_lance.py (NEW)
#   s2  code      scripts/_lib/pg_copy_text_parser.py (NEW) + tests
#   s3  code      app/services/lance_views.py (12 new LanceView entries)
#   s4  migration supabase/migrations/{ts}_usaspending_db_dump_ingest_ledger.sql
#   s5  migration supabase/migrations/{ts}_usaspending_lance_data_sources.sql
#   s6  config    Polaris Generic Table registrations x 12
#   s7  code      scripts/run_usaspending_db_dump_cycle.py (NEW)
#   s8  deploy    Railway data-engine-x auto-redeploy + runtime probe
#
# Usage:
#   ./2026-05-17-hq-all-usaspending-db-dump-multi-table-lance-ingest.sh             # pre-deploy structural checks
#   ./2026-05-17-hq-all-usaspending-db-dump-multi-table-lance-ingest.sh --surface s1
#   POPULATED=1 ./...                  # incl. Lance row-count + BTREE gates + DB checks + Polaris GETs
#   MERGE_SHA=<sha> ./...              # incl. Railway deploy SHA match + runtime probe

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

# --- 12 target tables (canonical PK + secondary BTREE keys per directive) ---
# Order matches directive §"Target tables (12)" — by dependency / size order.
# shellcheck disable=SC2034  # documentation; table list is also hardcoded in each loop body
TARGET_TABLES=(
  "subaward"
  "awards"
  "transaction_normalized"
  "transaction_fpds"
  "transaction_fabs"
  "recipient_lookup"
  "recipient_profile"
  "references_location"
  "toptier_agency"
  "subtier_agency"
  "agency"
  "references_cfda"
)

# Canonical PK per table (validated against dump's TOC by audit subagent
# REQUIRES_AUDIT: if USAspending has renamed any column since this directive
# was scoped, the audit subagent updates these lookup functions before finalizing).
#
# bash-3.2-compatible: no `declare -A` (macOS default ships bash 3.2).
_table_pk() {
  case "$1" in
    subaward)               echo "subaward_id" ;;
    awards)                 echo "id" ;;
    transaction_normalized) echo "id" ;;
    transaction_fpds)       echo "transaction_id" ;;
    transaction_fabs)       echo "transaction_id" ;;
    recipient_lookup)       echo "id" ;;
    recipient_profile)      echo "recipient_hash" ;;
    references_location)    echo "location_id" ;;
    toptier_agency)         echo "toptier_agency_id" ;;
    subtier_agency)         echo "subtier_agency_id" ;;
    agency)                 echo "id" ;;
    references_cfda)        echo "id" ;;
    *) echo "UNKNOWN_TABLE_$1" ;;
  esac
}

# Per-table MIN_ROW_FLOOR per directive §"Volume floors". Conservative for the
# three transaction tables; the audit subagent ground-truths against pg_restore -l
# at cycle start and may refine (REQUIRES_AUDIT).
_table_floor() {
  case "$1" in
    subaward)               echo 10000000 ;;
    awards)                 echo 40000000 ;;
    transaction_normalized) echo 250000000 ;;
    transaction_fpds)       echo 80000000 ;;
    transaction_fabs)       echo 150000000 ;;
    recipient_lookup)       echo 4000000 ;;
    recipient_profile)      echo 4000000 ;;
    references_location)    echo 8000000 ;;
    toptier_agency)         echo 100 ;;
    subtier_agency)         echo 1000 ;;
    agency)                 echo 1000 ;;
    references_cfda)        echo 2000 ;;
    *) echo 0 ;;
  esac
}

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

# --- Polaris generic-table existence check (shared helper) --------------- #
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

# ── s1: Modal app — usaspending_db_dump_to_r2_and_lance.py ─────────────── #
# Pattern A pull-through:
#  (1) Resolve latest dump URL via discovery API:
#      GET https://api.usaspending.gov/api/v2/bulk_download/list_database_download_files/
#      -> .full_download_file.url => usaspending-db_YYYYMMDD.zip on files.usaspending.gov
#  (2) Download .zip to /cache/<filename>.zip (~172 GB).
#  (3) Extract to /cache/usaspending_db/ (`pg_dump -Fd` directory: toc.dat + <n>.dat.gz files).
#  (4) For each TARGET_TABLES entry, spawn:
#        pg_restore --data-only -t <table> -f - /cache/usaspending_db/
#      Stream stdout through scripts/_lib/pg_copy_text_parser.py -> pyarrow RecordBatch
#      -> lance.write_dataset to s3://dex-raw-landing-zone/polaris-warehouse/usaspending/<table>_lance,
#      mode="overwrite". BTREE on canonical PK + secondary keys, compact + cleanup_old_versions.
#
# Q4 RESOLVED: URL pattern + ZIP/directory format frozen in header above.
# REQUIRES_AUDIT residual (executor at cycle start):
#  - Probe `pg_restore -l /cache/usaspending_db/` and verify TABLE_PK array matches
#    actual canonical PK column names per table (USAspending schema can evolve).
#  - Ground-truth TABLE_FLOOR values against actual row counts in the dump TOC.
#  - Resolve per-column PG-type -> Arrow-type mapping per table from the dump's TOC;
#    pick `list<large_string>` vs pipe-delimited `large_string` per L54 for any
#    unbounded-list column.
run_surface "s1" "hq-all" '
  f="$DEX_APP/modal/usaspending_db_dump_to_r2_and_lance.py"
  test -f "$f" &&
  # Modal app shape — assert decorator and constructor
  grep -qE "@app\.function" "$f" &&
  grep -qE "modal\.App" "$f" &&
  # Modal app name (must be data-engine-x-usaspending-db-dump-multi-table)
  grep -qE "data-engine-x-usaspending-db-dump-multi-table" "$f" &&
  # Resource floors per directive §"Surfaces" s1 row (memory=65536, cpu=8, timeout=21600)
  grep -qE "memory[[:space:]]*=[[:space:]]*65536" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*21600" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*8" "$f" &&
  # Modal Volume cache mount (usaspending-db-dump-cache at /cache)
  grep -qE "usaspending-db-dump-cache" "$f" &&
  grep -qE "/cache" "$f" &&
  # Standard FUNCTION_SECRETS pair
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  # Dump discovery via USAspending API (Q4-resolved URL pattern)
  grep -qE "api\.usaspending\.gov/api/v2/bulk_download/list_database_download_files" "$f" &&
  # ZIP archive download + extract (pg_dump -Fd directory format inside .zip)
  grep -qE "files\.usaspending\.gov/database_download/" "$f" &&
  grep -qE "usaspending-db_" "$f" &&
  grep -qE "\.zip" "$f" &&
  # pg_restore decoder invocation (subprocess.Popen --data-only against directory format)
  grep -qE "pg_restore" "$f" &&
  grep -qE "--data-only" "$f" &&
  grep -qE "subprocess\.Popen" "$f" &&
  # COPY-text parser import
  grep -qE "pg_copy_text_parser" "$f" &&
  # Lance write target prefix (one per table) — validate at least one and the prefix shape
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/" "$f" &&
  grep -qE "_lance" "$f" &&
  # All 12 target tables referenced in source
  for tbl in subaward awards transaction_normalized transaction_fpds transaction_fabs \
             recipient_lookup recipient_profile references_location toptier_agency \
             subtier_agency agency references_cfda; do
    grep -qE "\"$tbl\"|'"'"'$tbl'"'"'" "$f" || { echo "FAIL: table $tbl not referenced in $f" >&2; exit 1; }
  done &&
  # lance_commit_lock wrapper
  grep -qE "lance_commit_lock" "$f" &&
  # BTREE index creation
  grep -qE "create_scalar_index.*BTREE|index_type[[:space:]]*=[[:space:]]*[\"'"'"']BTREE[\"'"'"']" "$f" &&
  # Compact + cleanup discipline
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # DataFusion sort-spill bypass + tmpdir pinned
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  grep -qE "TMPDIR.*=.*/cache/lance-tmp|/cache/lance-tmp" "$f" &&
  # Detach launch documented (L47 — multi-hour job)
  grep -qE "modal run --detach" "$f" &&
  # Ledger writes (s4 table)
  grep -qE "ops\.usaspending_db_dump_ingest_runs" "$f" &&
  # Idempotency gate — HEAD-Last-Modified vs source_observed_at
  grep -qE "source_observed_at" "$f" &&
  grep -qE "(Last-Modified|HEAD)" "$f" &&
  # no_change status path
  grep -qE "'"'"'no_change'"'"'|no_change" "$f" &&
  # Pattern A NEGATIVE asserts: no register_match_method*, no register_bridge call
  ! grep -qE "register_match_method" "$f" &&
  ! grep -qE "register_bridge[[:space:]]*\\(" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\\.bridges" "$f" &&
  # L42 NEGATIVE assert: no Content-Encoding: zstd header on any R2 upload (this cycle has none, but defensive)
  ! grep -qE "Content-Encoding.*zstd" "$f" &&
  # @app.local_entrypoint() shape (def run, modal run --detach friendly)
  grep -qE "@app\.local_entrypoint" "$f" &&
  # POPULATED check: each Lance dataset exists + row floor + canonical PK BTREE present
  if [[ -n "${POPULATED:-}" ]]; then
    PASS_ALL=1
    for tbl in subaward awards transaction_normalized transaction_fpds transaction_fabs \
               recipient_lookup recipient_profile references_location toptier_agency \
               subtier_agency agency references_cfda; do
      uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/${tbl}_lance"
      pk=$(_table_pk "$tbl")
      floor=$(_table_floor "$tbl")
      _lance_floor_check "$uri" "$floor" || PASS_ALL=0
      _lance_btree_check "$uri" "$pk"    || PASS_ALL=0
    done
    [[ "$PASS_ALL" = "1" ]]
  else
    echo "   (POPULATED unset — skipping Lance row-count + BTREE gates for s1)"
    true
  fi
'

# ── s2: COPY-text parser library + unit tests ──────────────────────────── #
# scripts/_lib/pg_copy_text_parser.py — streaming parser for pg_restore
# --data-only -f - stdout. Yields pyarrow RecordBatch chunks.
#
# REQUIRES_AUDIT: audit subagent finalizes the exact test function names in
# tests/test_pg_copy_text_parser.py (the harness asserts file existence +
# pytest discovery + minimum 4 test cases covering NULL / embedded tabs /
# UTF-8 multibyte / typed columns per directive §"Surfaces" s2).
run_surface "s2" "hq-all" '
  f="$DEX_APP/scripts/_lib/pg_copy_text_parser.py"
  t="$DEX_APP/tests/test_pg_copy_text_parser.py"
  test -f "$f" &&
  # PG escape decoder presence
  grep -qE "\\\\\\\\N|\\\\N" "$f" &&  # \N null marker
  grep -qE "RecordBatch" "$f" &&
  grep -qE "import pyarrow|from pyarrow" "$f" &&
  # COPY marker parsing
  grep -qE "COPY[[:space:]]" "$f" &&
  # Batch chunk size config
  grep -qE "batch_size|chunk_size|50_000|50000" "$f" &&
  # Unit tests present
  test -f "$t" &&
  # Minimum 4 test cases (NULL, tabs, UTF-8, typed)
  grep -cE "^def test_" "$t" | (read n; [[ "$n" -ge 4 ]])
'

# ── s3: lance_views.py — 12 new LanceView entries, register_at_boot=False ──── #
# Per-key reads go through lance.dataset(uri).scanner(filter=...) directly.
# 12 new entries: usaspending_<table>_lance_raw for each table in TARGET_TABLES.
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  PASS_ALL=1
  for tbl in subaward awards transaction_normalized transaction_fpds transaction_fabs \
             recipient_lookup recipient_profile references_location toptier_agency \
             subtier_agency agency references_cfda; do
    grep -qE "usaspending_${tbl}_lance_raw" "$f" || { echo "FAIL: usaspending_${tbl}_lance_raw not in $f" >&2; PASS_ALL=0; }
    grep -qE "polaris-warehouse/usaspending/${tbl}_lance" "$f" || { echo "FAIL: polaris-warehouse/usaspending/${tbl}_lance URI not in $f" >&2; PASS_ALL=0; }
    # Boot ergonomics: register_at_boot=False adjacent to each new entry
    awk "/usaspending_${tbl}_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" || { echo "FAIL: register_at_boot=False missing adjacent to usaspending_${tbl}_lance_raw" >&2; PASS_ALL=0; }
  done
  [[ "$PASS_ALL" = "1" ]]
'

# ── s4: ops.usaspending_db_dump_ingest_runs ledger migration ──────────── #
run_surface "s4" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_usaspending_db_dump_ingest_ledger.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # Table shape per directive §"Surfaces" s4 row
  grep -qE "CREATE TABLE IF NOT EXISTS ops\.usaspending_db_dump_ingest_runs" "$mig" &&
  grep -qE "run_id[[:space:]]+uuid" "$mig" &&
  grep -qE "source_observed_at[[:space:]]+timestamptz" "$mig" &&
  grep -qE "source_download_url[[:space:]]+text[[:space:]]+NOT[[:space:]]+NULL" "$mig" &&
  grep -qE "started_at[[:space:]]+timestamptz" "$mig" &&
  grep -qE "completed_at[[:space:]]+timestamptz" "$mig" &&
  grep -qE "tables_extracted[[:space:]]+jsonb" "$mig" &&
  grep -qE "total_rows_written[[:space:]]+bigint" "$mig" &&
  # L4: CHECK constraint covers all 5 statuses
  grep -qE "CHECK[[:space:]]*\(status[[:space:]]+IN" "$mig" &&
  for st in pending running completed failed no_change; do
    grep -qE "'"'"'$st'"'"'" "$mig" || { echo "FAIL: status $st missing from CHECK constraint" >&2; exit 1; }
  done &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_usaspending_db_dump_ingest_ledger\.sql$"
'

# ── s4 (POPULATED): ledger table exists in live DB ────────────────────── #
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s4-applied" "hq-all" '
    result=$(dex_psql_query "SELECT to_regclass('"'"'ops.usaspending_db_dump_ingest_runs'"'"')::text") &&
    test "$result" = "ops.usaspending_db_dump_ingest_runs"
  '
fi

# ── s5: ops.data_sources migration — 12 INSERT rows, 5-col shape ─────── #
# Display names: usaspending_<table>_lance. L50: 5-col shape, NO `kind` column.
run_surface "s5" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_usaspending_lance_data_sources.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # 12 display_names present
  PASS_ALL=1
  for tbl in subaward awards transaction_normalized transaction_fpds transaction_fabs \
             recipient_lookup recipient_profile references_location toptier_agency \
             subtier_agency agency references_cfda; do
    grep -qE "usaspending_${tbl}_lance" "$mig" || { echo "FAIL: usaspending_${tbl}_lance not in $mig" >&2; PASS_ALL=0; }
    grep -qE "polaris-warehouse/usaspending/${tbl}_lance" "$mig" || { echo "FAIL: polaris-warehouse/usaspending/${tbl}_lance URI not in $mig" >&2; PASS_ALL=0; }
  done &&
  [[ "$PASS_ALL" = "1" ]] &&
  # 5-col shape: format=lance, owner_app=data-engine-x, status=active
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  grep -qE "'"'"'data-engine-x'"'"'" "$mig" &&
  grep -qE "'"'"'active'"'"'" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO UPDATE
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # L50 NEGATIVE assert: NO `kind` column in INSERT column list or VALUES
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\.data_sources[^;]*\(kind|,[[:space:]]*kind[[:space:]]*[,)]" "$mig" &&
  # Pattern A discipline: NO ops.bridges, NO register_match_method*.
  ! grep -qE "ops\.bridges" "$mig" &&
  ! grep -qE "register_match_method" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_usaspending_lance_data_sources\.sql$"
'

# ── s5 (POPULATED): explicit-list row count = 12 NEW ──────────────────── #
# Live DB already has 4 rows matching `usaspending_%_lance`; this check is an
# explicit-list match against the 12 NEW display_names this cycle adds.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s5-applied" "hq-all" '
    NAMES="'"'"'usaspending_subaward_lance'"'"', '"'"'usaspending_awards_lance'"'"', '"'"'usaspending_transaction_normalized_lance'"'"', '"'"'usaspending_transaction_fpds_lance'"'"', '"'"'usaspending_transaction_fabs_lance'"'"', '"'"'usaspending_recipient_lookup_lance'"'"', '"'"'usaspending_recipient_profile_lance'"'"', '"'"'usaspending_references_location_lance'"'"', '"'"'usaspending_toptier_agency_lance'"'"', '"'"'usaspending_subtier_agency_lance'"'"', '"'"'usaspending_agency_lance'"'"', '"'"'usaspending_references_cfda_lance'"'"'"
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ($NAMES) AND status='"'"'active'"'"' AND format='"'"'lance'"'"' AND owner_app='"'"'data-engine-x'"'"'")
    test "$COUNT" = "12"
  '
fi

# ── s6: Polaris Generic Tables — 12 registrations ────────────────────── #
# Namespace: usaspending. Tables: <table>_lance for each TARGET_TABLES entry.
if [[ -n "${POPULATED:-}" ]]; then
  for tbl in subaward awards transaction_normalized transaction_fpds transaction_fabs \
             recipient_lookup recipient_profile references_location toptier_agency \
             subtier_agency agency references_cfda; do
    run_surface "s6-${tbl}" "hq-all" "_polaris_lance_check usaspending ${tbl}_lance"
  done
else
  echo "-- s6 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET checks for 12 generic tables)"
  SKIP_COUNT=$((SKIP_COUNT+12))
fi

# ── s7: CLI wrapper run_usaspending_db_dump_cycle.py ──────────────────── #
# Thin argparse CLI that:
#   - resolves the latest dump URL via the USAspending discovery API
#     (https://api.usaspending.gov/api/v2/bulk_download/list_database_download_files/)
#   - accepts --dump-url override and --check-only (echo resolved URL + Modal cmd, no execute)
#   - exec's `modal run --detach apps/data-engine-x/modal/usaspending_db_dump_to_r2_and_lance.py::run`
run_surface "s7" "hq-all" '
  f="$DEX_APP/scripts/run_usaspending_db_dump_cycle.py"
  test -f "$f" &&
  # Modal app invocation shape
  grep -qE "modal run --detach" "$f" &&
  grep -qE "apps/data-engine-x/modal/usaspending_db_dump_to_r2_and_lance\.py" "$f" &&
  # --dump-url override + --check-only flags per directive §"Surfaces" s7 row
  grep -qE "--dump-url" "$f" &&
  grep -qE "--check-only" "$f" &&
  # Discovery API URL referenced for the resolve-latest path
  grep -qE "api\.usaspending\.gov/api/v2/bulk_download/list_database_download_files" "$f"
'

# ── s8: Railway data-engine-x deploy status + runtime probe ───────────── #
# This cycle does NOT add new endpoints (only register_at_boot=False LanceView
# entries; boot imports them but does not materialize). Default runtime probe
# per apps/data-engine-x/CLAUDE.md §"Deploy verification".
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s8-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd \"$HQ_ALL_ROOT/apps/data-engine-x\" && \
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-usasp-db-dump-sha.\$\$
      actual_sha=\$(cat /tmp/dex-usasp-db-dump-sha.\$\$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-usasp-db-dump-sha.\$\$
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
  run_surface "s8-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s8 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# ── Verification harness extra checks (directive §"Verification harness") ── #
# Check #8 from directive: sample DuckDB-over-Lance query against `subaward`
# confirms subawardee_highly_compensated_officer_1_name is non-NULL for ≥1 row.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "v8-officer-name-sample" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      uv run --quiet --with duckdb --with pylance python3 -c \"
import os, sys, duckdb, lance
storage_options = {
    \\\"aws_endpoint\\\": os.environ[\\\"R2_ENDPOINT\\\"],
    \\\"aws_access_key_id\\\": os.environ[\\\"R2_ACCESS_KEY_ID\\\"],
    \\\"aws_secret_access_key\\\": os.environ[\\\"R2_SECRET_ACCESS_KEY\\\"],
    \\\"aws_region\\\": \\\"us-east-1\\\",
    \\\"aws_virtual_hosted_style_request\\\": \\\"false\\\",
}
ds = lance.dataset(\\\"s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance\\\", storage_options=storage_options)
con = duckdb.connect()
con.register(\\\"subaward\\\", ds.to_table())
row = con.execute(\\\"SELECT COUNT(*) FROM subaward WHERE subawardee_highly_compensated_officer_1_name IS NOT NULL\\\").fetchone()[0]
if row >= 1:
    print(f\\\"PASS: subawardee_highly_compensated_officer_1_name non-NULL rows={row:,}\\\")
    sys.exit(0)
print(f\\\"FAIL: subawardee_highly_compensated_officer_1_name has zero non-NULL rows\\\")
sys.exit(1)
\"
    "
  '
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
