#!/usr/bin/env bash
# Verification harness for directive: 2026-05-04-source-warn-notices-nv
# Audit-filled. See /Users/benjamincrane/Desktop/hq/directives/2026-05-04-source-warn-notices-nv.md
# §"Audit plan" for the per-surface rationale.
#
# Surfaces (4, single repo bencrane/hq-all -> apps/data-engine-x):
#   s1  code    apps/data-engine-x/scripts/run_warn_notices_ingest.py - ingest_nv() PDF adapter
#               (detr.nv.gov/Page/WARN HTML index -> per-year master PDFs -> pdfplumber row extract)
#               + apps/data-engine-x/pyproject.toml - add pdfplumber>=0.11.0
#               + source_provider literal 'nv_warn_notices'
#   s2  code    apps/data-engine-x/scripts/run_warn_notices_ingest.py
#               + apps/data-engine-x/modal/warn_notices_ingest_app.py - --state nv accepted
#               (_ENABLED_STATES = ("fl","tx","nj","nv"); main() dispatch wired)
#   s3  deploy  Modal cloud - `data-engine-x-warn-notices-ingest` redeploy post-merge
#               (picks up pdfplumber dep from pyproject.toml)
#   s4  deploy  prod DB - entities.source_warn_notices rows for state='NV'
#               via `modal run --state nv` backfill
#
# Doppler idiom (per apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha"):
#   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -c "..."'
#
# Helper functions (dex_provenance_check, dex_min_row_floor_check, dex_psql_query)
# come from apps/data-engine-x/scripts/_lib/dex.sh - sourced via the vault thin shim.
#
# Usage:
#   ./source-warn-notices-nv.sh
#   ./source-warn-notices-nv.sh --surface s1
#   ./source-warn-notices-nv.sh --skip-volume-floor      # pre-backfill pass
#
# Volume floors (per directive):
#   entities.source_warn_notices WHERE state='NV' >= 80
#   (validator probe: 5 master PDFs, 195 date-prefixed rows total across
#    2022/2023/2024/2025/current; comfortably exceeds floor)
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

echo "==> Verifying source-warn-notices-nv surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all}, skip-volume-floor: $SKIP_VOLUME_FLOOR)"

# -------------------------------------------------------------------------- #
# s1 - code: ingest_nv() PDF adapter + pdfplumber dependency.
# Required:
#   * function defined: def ingest_nv(...)
#   * uses _fetch_html (Slice A hardening) for index + PDFs
#   * fetches detr.nv.gov index page
#   * uses pdfplumber for table extraction
#   * pyproject.toml lists pdfplumber>=0.11.0 (alphabetical positioning is
#     not asserted; Slice A's existing list is not strictly alphabetical)
#   * raw_source_row preserved (per CLAUDE.md §"Source ingest invariant")
#   * ON CONFLICT upsert (re-uses Slice A's _UPSERT_SQL unchanged)
#   * NV state literal stamped on inserted rows
#   * source_provider = 'nv_warn_notices' (matches FL/NJ precedent)
# -------------------------------------------------------------------------- #
run_surface "s1" "data-engine-x" 'cd "$HQ_ALL" && \
  git ls-files --error-unmatch '"$INGEST_PY"' >/dev/null 2>&1 && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  grep -Eq "def[[:space:]]+ingest_nv" '"$INGEST_PY"' && \
  grep -q "detr.nv.gov" '"$INGEST_PY"' && \
  grep -Eq "import[[:space:]]+pdfplumber|from[[:space:]]+pdfplumber" '"$INGEST_PY"' && \
  grep -Eq "_fetch_html|_DEFAULT_HEADERS" '"$INGEST_PY"' && \
  grep -q "raw_source_row" '"$INGEST_PY"' && \
  grep -q "ON CONFLICT" '"$INGEST_PY"' && \
  grep -q "\"NV\"" '"$INGEST_PY"' && \
  grep -q "nv_warn_notices" '"$INGEST_PY"' && \
  grep -Eq "pdfplumber[[:space:]]*>=[[:space:]]*0\.11" '"$PYPROJECT"''

# -------------------------------------------------------------------------- #
# s2 - code: --state nv accepted; _ENABLED_STATES extended; main() dispatch
# wired (proves ingest_nv() is reachable, not just defined).
# -------------------------------------------------------------------------- #
run_surface "s2" "data-engine-x" 'cd "$HQ_ALL" && \
  python3 -c "import ast; ast.parse(open(\"'"$INGEST_PY"'\").read())" && \
  python3 -c "import ast; ast.parse(open(\"'"$MODAL_PY"'\").read())" && \
  grep -Eq "_ENABLED_STATES[[:space:]]*=[[:space:]]*\([[:space:]]*\"fl\"[[:space:]]*,[[:space:]]*\"tx\"[[:space:]]*,[[:space:]]*\"nj\"[[:space:]]*,[[:space:]]*\"nv\"" '"$INGEST_PY"' && \
  grep -q "ingest_nv" '"$INGEST_PY"' && \
  grep -Eq "st[[:space:]]*==[[:space:]]*\"nv\"" '"$INGEST_PY"' && \
  grep -Eq "(--state|\"--state\")" '"$INGEST_PY"' && \
  grep -q "\"--state\"" '"$MODAL_PY"''

# -------------------------------------------------------------------------- #
# s3 - deploy: Modal app redeployed with pdfplumber in image; secret unchanged.
# -------------------------------------------------------------------------- #
run_surface "s3" "data-engine-x" 'modal secret list --json | jq -e '"'"'.[] | select((.Name // .name) == "warn-notices-db")'"'"' >/dev/null && \
  modal app list --json | jq -e '"'"'.[] | select(((.["Description"] // .name) == "data-engine-x-warn-notices-ingest") and (((.["State"] // .state) | ascii_downcase) == "deployed"))'"'"' >/dev/null'

# -------------------------------------------------------------------------- #
# s4 - deploy: NV row volume floor + provenance + idempotent run history.
#   NV >= 80. Skip with --skip-volume-floor on the pre-backfill pass.
#   Validator probe baseline: 195 date-prefixed rows across 5 master PDFs;
#   after dedup of (year-pinned ∩ current-cumulative) overlap, conservatively
#   100-195 unique rows expected.
# -------------------------------------------------------------------------- #
if [[ $SKIP_VOLUME_FLOOR -eq 1 ]]; then
  run_surface "s4" "data-engine-x" 'echo "s4: SKIPPED volume floor (pre-backfill pass)"'
else
  run_surface "s4" "data-engine-x" '\
    nv_cnt=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state='"'"'NV'"'"'" | tr -d "[:space:]") && \
    echo "s4 counts: NV=$nv_cnt" && \
    [[ "$nv_cnt" =~ ^[0-9]+$ ]] && (( nv_cnt >= 80 )) && \
    nv_provider=$(dex_psql_query "SELECT count(*) FROM entities.source_warn_notices WHERE state='"'"'NV'"'"' AND source_provider='"'"'nv_warn_notices'"'"'" | tr -d "[:space:]") && \
    [[ "$nv_provider" =~ ^[0-9]+$ ]] && (( nv_provider >= 80 )) && \
    [[ "$(dex_psql_query "SELECT status FROM ops.warn_notices_ingest_runs WHERE state='"'"'NV'"'"' ORDER BY started_at DESC LIMIT 1" | tr -d "[:space:]")" == "succeeded" ]]'
fi

echo "==> All requested surfaces verified."
