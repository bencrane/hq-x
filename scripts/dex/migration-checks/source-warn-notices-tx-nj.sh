#!/usr/bin/env bash
# Verification harness for directive: 2026-05-04-source-warn-notices-tx-nj
# Filled in by audit subagent.
#
# Surfaces (6, single repo bencrane/hq-all -> apps/data-engine-x):
#   s1  code    apps/data-engine-x/scripts/run_warn_notices_ingest.py - shared fetch helper hardening
#               (Mozilla UA constant + retry/backoff loop + per-request sleep ~0.5s; introduces _fetch_json)
#   s2  code   .../scripts/run_warn_notices_ingest.py - ingest_tx() Socrata adapter
#               (data.texas.gov/resource/8w53-c4f6.json, $limit/$offset, sha256 PK over 5 stable cols)
#   s3  code   .../scripts/run_warn_notices_ingest.py - ingest_nj() XLSX adapter + pyproject openpyxl dep
#               (single XLSX, 23 sheets, sha256 PK including sheet_year)
#   s4  code   .../scripts/run_warn_notices_ingest.py + modal/warn_notices_ingest_app.py - --state arg
#               (accept tx, nj in addition to fl; modal run_ingest passes through)
#   s5  deploy Modal cloud - `data-engine-x-warn-notices-ingest` redeploy post-merge
#   s6  deploy prod DB - TX + NJ + FL re-backfill rows in entities.source_warn_notices
#
# Doppler idiom (per apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha"):
#   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -c "..."'
#
# Helper functions (dex_provenance_check, dex_min_row_floor_check, dex_psql_query)
# come from apps/data-engine-x/scripts/_lib/dex.sh - sourced via the vault thin shim.
#
# Usage:
#   ./source-warn-notices-tx-nj.sh
#   ./source-warn-notices-tx-nj.sh --surface s1
#   ./source-warn-notices-tx-nj.sh --repo data-engine-x
#   ./source-warn-notices-tx-nj.sh --skip-volume-floor      # pre-backfill pass
#
# Volume floors (per directive):
#   entities.source_warn_notices WHERE state='TX' >= 300   (Socrata reports 2,340)
#   entities.source_warn_notices WHERE state='NJ' >= 200   (XLSX reports ~2,320)
#   entities.source_warn_notices WHERE state='FL' >= 500   (pre-existing FL floor; this slice unblocks)
#   entities.source_warn_notices (TX+NJ combined)        >= 500
#
# Exits 0 only if every requested surface passes.

set -euo pipefail

SURFACE_FILTER=""
REPO_FILTER=""
SKIP_VOLUME_FLOOR=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)            SURFACE_FILTER="$2"; shift 2 ;;
    --repo)               REPO_FILTER="$2";    shift 2 ;;
    --skip-volume-floor)  SKIP_VOLUME_FLOOR=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

HQ_ALL="${HQ_ALL:-/Users/benjamincrane/hq-all}"
APP_DIR="$HQ_ALL/apps/data-engine-x"

# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

REMOTE=$(git -C "$HQ_ALL" remote get-url origin 2>&1 || echo "MISSING")
if [[ "$REMOTE" != *"bencrane/hq-all"* ]]; then
  echo "FAIL: $HQ_ALL origin is '$REMOTE' - expected bencrane/hq-all" >&2
  exit 1
fi

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (surface filter)"
    return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

INGEST_PY="apps/data-engine-x/scripts/run_warn_notices_ingest.py"
MODAL_PY="apps/data-engine-x/modal/warn_notices_ingest_app.py"
PYPROJECT="apps/data-engine-x/pyproject.toml"

echo "==> Verifying source-warn-notices-tx-nj surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all}, skip-volume-floor: $SKIP_VOLUME_FLOOR)"

# -------------------------------------------------------------------------- #
# s1 - code: shared fetch helper hardening (UA + retry/backoff + sleep, plus
# new _fetch_json helper used by the TX Socrata adapter).
# -------------------------------------------------------------------------- #
run_surface "s1" "data-engine-x" 'cd "$HQ_ALL" && \
  git ls-files --error-unmatch '"$INGEST_PY"' >/dev/null 2>&1 && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  grep -Eq "Mozilla/5\.0" '"$INGEST_PY"' && \
  grep -q "AppleWebKit" '"$INGEST_PY"' && \
  grep -Eq "def[[:space:]]+_fetch_html" '"$INGEST_PY"' && \
  grep -Eq "def[[:space:]]+_fetch_json" '"$INGEST_PY"' && \
  grep -Eq "(403|429)" '"$INGEST_PY"' && \
  grep -Eq "(backoff|2 \*\* )" '"$INGEST_PY"' && \
  grep -q "max_attempts" '"$INGEST_PY"' && \
  grep -q "time.sleep" '"$INGEST_PY"''

# -------------------------------------------------------------------------- #
# s2 - code: ingest_tx() Socrata adapter.
# Includes a guard that the FL-hardcoded _upsert_rows() was either replaced
# by a TX/NJ-aware helper (_upsert_tx_nj_batch) OR refactored to accept a
# state arg — otherwise TX rows would silently land with FL provenance.
# -------------------------------------------------------------------------- #
run_surface "s2" "data-engine-x" 'cd "$HQ_ALL" && \
  git ls-files --error-unmatch '"$INGEST_PY"' >/dev/null 2>&1 && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  grep -Eq "def[[:space:]]+ingest_tx" '"$INGEST_PY"' && \
  grep -q "data.texas.gov" '"$INGEST_PY"' && \
  grep -q "8w53-c4f6" '"$INGEST_PY"' && \
  grep -q "\$limit" '"$INGEST_PY"' && \
  grep -q "\$offset" '"$INGEST_PY"' && \
  grep -q "tx_socrata" '"$INGEST_PY"' && \
  grep -q "raw_source_row" '"$INGEST_PY"' && \
  grep -q "ON CONFLICT" '"$INGEST_PY"' && \
  grep -q "TX" '"$INGEST_PY"' && \
  ( grep -q "_upsert_tx_nj_batch" '"$INGEST_PY"' || \
    grep -Eq "_upsert_rows\(.*[\"]TX[\"]" '"$INGEST_PY"' )'

# -------------------------------------------------------------------------- #
# s3 - code: ingest_nj() XLSX adapter + pyproject openpyxl dep + 23-sheet handling.
# -------------------------------------------------------------------------- #
run_surface "s3" "data-engine-x" 'cd "$HQ_ALL" && \
  git ls-files --error-unmatch '"$INGEST_PY"' >/dev/null 2>&1 && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  grep -Eq "def[[:space:]]+ingest_nj" '"$INGEST_PY"' && \
  grep -q "WARN_Notice_Archive.xlsx" '"$INGEST_PY"' && \
  grep -q "nj.gov/labor" '"$INGEST_PY"' && \
  grep -Eq "import[[:space:]]+openpyxl|from[[:space:]]+openpyxl" '"$INGEST_PY"' && \
  grep -q "load_workbook" '"$INGEST_PY"' && \
  grep -q "sheetnames" '"$INGEST_PY"' && \
  grep -q "sheet_year" '"$INGEST_PY"' && \
  grep -q "nj_warn_notices" '"$INGEST_PY"' && \
  ( grep -q "_upsert_tx_nj_batch" '"$INGEST_PY"' || \
    grep -Eq "_upsert_rows\(.*[\"]NJ[\"]" '"$INGEST_PY"' ) && \
  grep -Eq "openpyxl[[:space:]]*>=[[:space:]]*3\.1" '"$PYPROJECT"''

# -------------------------------------------------------------------------- #
# s4 - code: --state arg accepts {fl,tx,nj}; Modal run_ingest passes through.
# -------------------------------------------------------------------------- #
run_surface "s4" "data-engine-x" 'cd "$HQ_ALL" && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  python3 -c "import ast; ast.parse(open(\"'"$MODAL_PY"'\").read())" && \
  grep -Eq "(--state|\"--state\")" '"$INGEST_PY"' && \
  grep -Eq "(\"fl\"|.fl.).*(\"tx\"|.tx.).*(\"nj\"|.nj.)" '"$INGEST_PY"' && \
  grep -q "ingest_tx" '"$INGEST_PY"' && \
  grep -q "ingest_nj" '"$INGEST_PY"' && \
  grep -q "ingest_fl" '"$INGEST_PY"' && \
  grep -Eq "state(:[[:space:]]*str)?[[:space:]]*[=:][[:space:]]*(None|\"\")" '"$MODAL_PY"' && \
  grep -q "\"--state\"" '"$MODAL_PY"''

# -------------------------------------------------------------------------- #
# s5 - deploy: Modal app redeployed (data-engine-x-warn-notices-ingest) and
# named secret warn-notices-db still present. (Smoke run is operator-driven
# pre-merge; harness only asserts the deployed-state of the app.)
# -------------------------------------------------------------------------- #
run_surface "s5" "data-engine-x" 'modal secret list --json | jq -e '"'"'.[] | select((.Name // .name) == "warn-notices-db")'"'"' >/dev/null && \
  modal app list --json | jq -e '"'"'.[] | select(((.["Description"] // .name) == "data-engine-x-warn-notices-ingest") and (((.["State"] // .state) | ascii_downcase) == "deployed"))'"'"' >/dev/null'

# -------------------------------------------------------------------------- #
# s6 - deploy: row-volume floors per state.
#   TX>=300, NJ>=200, FL>=500, combined TX+NJ>=500.
#   Skip with --skip-volume-floor on the pre-backfill pass.
# -------------------------------------------------------------------------- #
if [[ $SKIP_VOLUME_FLOOR -eq 1 ]]; then
  run_surface "s6" "data-engine-x" 'echo "s6: SKIPPED volume floor (pre-backfill pass)"'
else
  run_surface "s6" "data-engine-x" '\
    tx_cnt=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state='"'"'TX'"'"'" | tr -d "[:space:]") && \
    nj_cnt=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state='"'"'NJ'"'"'" | tr -d "[:space:]") && \
    fl_cnt=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state='"'"'FL'"'"'" | tr -d "[:space:]") && \
    combined_cnt=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state IN ('"'"'TX'"'"','"'"'NJ'"'"')" | tr -d "[:space:]") && \
    echo "s6 counts: TX=$tx_cnt NJ=$nj_cnt FL=$fl_cnt TX+NJ=$combined_cnt" && \
    [[ "$tx_cnt" =~ ^[0-9]+$ ]] && (( tx_cnt >= 300 )) && \
    [[ "$nj_cnt" =~ ^[0-9]+$ ]] && (( nj_cnt >= 200 )) && \
    [[ "$fl_cnt" =~ ^[0-9]+$ ]] && (( fl_cnt >= 500 )) && \
    [[ "$combined_cnt" =~ ^[0-9]+$ ]] && (( combined_cnt >= 500 )) && \
    [[ "$(dex_psql_query "SELECT status FROM ops.warn_notices_ingest_runs WHERE state='"'"'TX'"'"' ORDER BY started_at DESC LIMIT 1")" == "succeeded" ]] && \
    [[ "$(dex_psql_query "SELECT status FROM ops.warn_notices_ingest_runs WHERE state='"'"'NJ'"'"' ORDER BY started_at DESC LIMIT 1")" == "succeeded" ]]'
fi

echo "==> All requested surfaces verified."
