#!/usr/bin/env bash
# Canonical helpers for data-engine-x migration verifiers, ingest scripts, and
# /scope harness scripts. Source this file; never re-encode the Doppler `bash -c`
# wrapper or the DEX_DB_URL_DIRECT-vs-POOLED choice inline.
#
# See apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha" and §"Helper library".
#
# Sourced (not executed) — guard against direct invocation.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "dex.sh is a library — source it, don't execute it" >&2
  exit 1
fi

DEX_DOPPLER_PROJECT="${DEX_DOPPLER_PROJECT:-hq-all}"
DEX_DOPPLER_CONFIG="${DEX_DOPPLER_CONFIG:-prd}"

# --- Doppler-wrapped invocation: defers $VAR expansion to the injected env -- #
_dex_doppler() {
  doppler run --project "$DEX_DOPPLER_PROJECT" --config "$DEX_DOPPLER_CONFIG" -- bash -c "$1"
}

# --- DDL: $DEX_DB_URL_DIRECT (pgbouncer transaction-mode at the pooled URL
#     blocks DDL and REFRESH MATERIALIZED VIEW CONCURRENTLY) ---------------- #
dex_psql_ddl() {
  local stmt="$1"
  _dex_doppler "psql \"\$DEX_DB_URL_DIRECT\" -v ON_ERROR_STOP=1 -c \"$stmt\""
}

dex_psql_ddl_file() {
  local path="$1"
  # Disable Postgres-level statement_timeout for the duration of this psql
  # session. Long migrations (e.g. multi-million-row INSERT...SELECT
  # backfills, REFRESH MATERIALIZED VIEW on big MVs) blow past the role
  # default and get cancelled mid-statement otherwise. -c runs first and
  # the SET persists for the rest of the session that runs -f.
  _dex_doppler "psql \"\$DEX_DB_URL_DIRECT\" -v ON_ERROR_STOP=1 -c 'SET statement_timeout = 0;' -f '$path'"
}

# --- Read-only query: $DEX_DB_URL_POOLED (cheap, transaction-mode is fine) - #
dex_psql_query() {
  local sql="$1"
  _dex_doppler "psql \"\$DEX_DB_URL_POOLED\" -tAc \"$sql\""
}

# --- Read-only query against $DEX_DB_URL_DIRECT with no statement_timeout - #
# For analytical queries that exceed the pooled connection's default timeout
# (typical case: cross-source cluster aggregations across 10M+ MV rows with
# JOIN + GROUP BY). Verifiers should reach for this when dex_psql_query times
# out on heavy analytical work; reserve dex_psql_query for cheap point reads.
dex_psql_query_direct() {
  local sql="$1"
  _dex_doppler "psql \"\$DEX_DB_URL_DIRECT\" -v ON_ERROR_STOP=1 -tAc \"SET statement_timeout = 0; $sql\""
}

# --- Row count helper ---------------------------------------------------- #
dex_count_rows() {
  local table="$1"
  dex_psql_query "SELECT COUNT(*)::bigint FROM $table"
}

# --- MIN_ROW_FLOOR check: exit 0 iff COUNT(*) >= floor ------------------- #
# Source-ingest verifiers MUST call this (per /scope SKILL.md §"Volume floors").
# Floor comes from the directive's `## Volume floors` table — typically the
# upstream's published row count, or smoke-fixture × 1000 as a sanity floor.
dex_min_row_floor_check() {
  local table="$1" floor="$2"
  local actual
  actual=$(dex_count_rows "$table") || return 1
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: $table has $actual rows >= floor $floor"
    return 0
  fi
  echo "FAIL: $table has $actual rows < floor $floor" >&2
  return 1
}

# --- raw_source_row column check: exists and (optionally) populated ------ #
dex_raw_source_row_check() {
  local table="$1"
  local schema_name="${table%.*}"
  local table_name="${table#*.}"
  local exists
  exists=$(dex_psql_query "SELECT 1 FROM information_schema.columns WHERE table_schema='$schema_name' AND table_name='$table_name' AND column_name='raw_source_row'")
  if [[ "$exists" != "1" ]]; then
    echo "FAIL: $table missing raw_source_row column" >&2
    return 1
  fi
  echo "PASS: $table has raw_source_row column"
  return 0
}

# --- Full canonical-provenance check (8 cols + raw_source_row) ----------- #
# Per apps/data-engine-x/CLAUDE.md §"Source ingest invariant".
dex_provenance_check() {
  local table="$1"
  local schema_name="${table%.*}"
  local table_name="${table#*.}"
  local missing=()
  local cols=(raw_source_row source_provider source_filename source_download_url source_observed_at source_run_metadata source_task_id source_schedule_id ingested_at)
  for col in "${cols[@]}"; do
    local present
    present=$(dex_psql_query "SELECT 1 FROM information_schema.columns WHERE table_schema='$schema_name' AND table_name='$table_name' AND column_name='$col'")
    if [[ "$present" != "1" ]]; then
      missing+=("$col")
    fi
  done
  if (( ${#missing[@]} == 0 )); then
    echo "PASS: $table has full canonical provenance"
    return 0
  fi
  echo "FAIL: $table missing provenance columns: ${missing[*]}" >&2
  return 1
}

# --- Idempotency check: run ingest twice, assert COUNT unchanged --------- #
# Pass the ingest invocation as a single string. Both runs use the same args.
dex_idempotency_check() {
  local ingest_cmd="$1" table="$2"
  echo "==> dex_idempotency_check: first run"
  eval "$ingest_cmd" >&2 || return 1
  local after_first
  after_first=$(dex_count_rows "$table") || return 1
  echo "==> dex_idempotency_check: second run"
  eval "$ingest_cmd" >&2 || return 1
  local after_second
  after_second=$(dex_count_rows "$table") || return 1
  if [[ "$after_first" == "$after_second" ]]; then
    echo "PASS: $table idempotent ($after_first rows after both runs)"
    return 0
  fi
  echo "FAIL: $table not idempotent ($after_first → $after_second)" >&2
  return 1
}
