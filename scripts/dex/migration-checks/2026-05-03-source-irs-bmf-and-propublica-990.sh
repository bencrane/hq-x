#!/usr/bin/env bash
# Verification harness for directive: 2026-05-03-source-irs-bmf-and-propublica-990
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (4 — s5 removed per operator decision [2]):
#   s1  migration  apps/data-engine-x/supabase/migrations/20260503120000_source_irs_bmf.sql
#                  (creates entities.source_irs_bmf + ops.irs_bmf_ingest_runs)
#   s2  migration  apps/data-engine-x/supabase/migrations/20260503130000_source_propublica_nonprofits.sql
#                  (creates entities.source_propublica_nonprofits + ops.propublica_nonprofit_ingest_runs)
#   s3  code       apps/data-engine-x/scripts/run_irs_bmf_ingest.py
#   s4  code       apps/data-engine-x/scripts/run_propublica_nonprofit_ingest.py
#
# Doppler idiom (from apps/data-engine-x/CLAUDE.md §"Doppler Shell Gotcha"):
#   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -c "..."'
# DDL connection MUST use DEX_DB_URL_DIRECT (not POOLED — pgbouncer transaction
# mode can't run DDL / REFRESH MV CONCURRENTLY).
#
# Doppler project/config is pinned per-directory via apps/data-engine-x/doppler.yaml
# (project=data-engine-x, config=prd). Harness `cd`s into APP_DIR before running
# doppler so the pin is honored.
#
# Usage: ./2026-05-03-source-irs-bmf-and-propublica-990.sh \
#          [--repo bencrane/hq-all] [--surface s1..s4]

set -euo pipefail

REPO_FILTER=""
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

HQ_ALL="/Users/benjamincrane/hq-all"
APP_DIR="$HQ_ALL/apps/data-engine-x"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

REMOTE=$(git -C "$HQ_ALL" remote get-url origin 2>&1 || echo "MISSING")
if [[ "$REMOTE" != *"bencrane/hq-all"* ]]; then
  echo "FAIL: $HQ_ALL origin is '$REMOTE' — expected bencrane/hq-all" >&2
  exit 1
fi

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
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

echo "==> Verifying source_irs_bmf + source_propublica_nonprofits surfaces (filter: ${REPO_FILTER:-all})"

# ---------------------------------------------------------------------------- #
# s1: migration — entities.source_irs_bmf  +  ops.irs_bmf_ingest_runs
# Asserts: source table exists, raw_source_row column present, PK = ein,
#          column count = 39 (28 BMF cols + 1 raw_source_row + 7 provenance
#          + ingested_at + created_at + updated_at), ops.irs_bmf_ingest_runs
#          sibling exists.
# ---------------------------------------------------------------------------- #
S1_CHECK='
  cd "$APP_DIR" && \
  doppler run -- bash -c '"'"'
    set -e
    EXISTS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_irs_bmf'"'"'"'"'"'"'"'"';
    ")
    [ "$EXISTS" = "1" ] || { echo "table missing"; exit 1; }
    COLS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM information_schema.columns
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_irs_bmf'"'"'"'"'"'"'"'"';
    ")
    [ "$COLS" = "39" ] || { echo "expected 39 columns, got $COLS"; exit 1; }
    HAS_RAW=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.columns
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_irs_bmf'"'"'"'"'"'"'"'"'
          AND column_name  = '"'"'"'"'"'"'"'"'raw_source_row'"'"'"'"'"'"'"'"';
    ")
    [ "$HAS_RAW" = "1" ] || { echo "raw_source_row missing"; exit 1; }
    PK=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = '"'"'"'"'"'"'"'"'entities.source_irs_bmf'"'"'"'"'"'"'"'"'::regclass
          AND i.indisprimary;
    ")
    [ "$PK" = "ein" ] || { echo "expected PK ein, got $PK"; exit 1; }
    RUNS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'irs_bmf_ingest_runs'"'"'"'"'"'"'"'"';
    ")
    [ "$RUNS" = "1" ] || { echo "ops.irs_bmf_ingest_runs missing"; exit 1; }
    echo "s1 ok"
  '"'"'
'

# ---------------------------------------------------------------------------- #
# s2: migration — entities.source_propublica_nonprofits  +
#                 ops.propublica_nonprofit_ingest_runs
# Asserts: source table exists, raw_source_row present, PK is composite
# (ein, tax_prd, formtype), ops.propublica_nonprofit_ingest_runs sibling exists.
# (Column count not asserted here because ProPublica's API surface evolves and
#  the audit plan tolerates ALTER TABLE ADD COLUMN IF NOT EXISTS for new keys —
#  raw_source_row jsonb is the safety net.)
# ---------------------------------------------------------------------------- #
S2_CHECK='
  cd "$APP_DIR" && \
  doppler run -- bash -c '"'"'
    set -e
    EXISTS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_propublica_nonprofits'"'"'"'"'"'"'"'"';
    ")
    [ "$EXISTS" = "1" ] || { echo "table missing"; exit 1; }
    HAS_RAW=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.columns
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_propublica_nonprofits'"'"'"'"'"'"'"'"'
          AND column_name  = '"'"'"'"'"'"'"'"'raw_source_row'"'"'"'"'"'"'"'"';
    ")
    [ "$HAS_RAW" = "1" ] || { echo "raw_source_row missing"; exit 1; }
    PK=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT string_agg(a.attname, '"'"'"'"'"'"'"'"','"'"'"'"'"'"'"'"' ORDER BY array_position(i.indkey, a.attnum))
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = '"'"'"'"'"'"'"'"'entities.source_propublica_nonprofits'"'"'"'"'"'"'"'"'::regclass
          AND i.indisprimary;
    ")
    [ "$PK" = "ein,tax_prd,formtype" ] || { echo "expected PK (ein,tax_prd,formtype), got $PK"; exit 1; }
    RUNS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'propublica_nonprofit_ingest_runs'"'"'"'"'"'"'"'"';
    ")
    [ "$RUNS" = "1" ] || { echo "ops.propublica_nonprofit_ingest_runs missing"; exit 1; }
    echo "s2 ok"
  '"'"'
'

# ---------------------------------------------------------------------------- #
# s3: code — scripts/run_irs_bmf_ingest.py exists, parses, hits ON CONFLICT (ein)
# + raw_source_row, plus 2-run idempotency stress test against fixture.
# Fixture: apps/data-engine-x/tests/fixtures/irs_bmf_smoke.csv (executor adds it)
# ---------------------------------------------------------------------------- #
S3_CHECK='
  test -f "$APP_DIR/scripts/run_irs_bmf_ingest.py" || { echo "script missing"; exit 1; }
  python3 -c "import ast; ast.parse(open('"'"'$APP_DIR/scripts/run_irs_bmf_ingest.py'"'"').read())" || { echo "syntax error"; exit 1; }
  grep -q "ON CONFLICT (ein)" "$APP_DIR/scripts/run_irs_bmf_ingest.py" || { echo "missing ON CONFLICT (ein)"; exit 1; }
  grep -q "raw_source_row"   "$APP_DIR/scripts/run_irs_bmf_ingest.py" || { echo "missing raw_source_row"; exit 1; }
  grep -q "irs_bmf"          "$APP_DIR/scripts/run_irs_bmf_ingest.py" || { echo "missing source_provider stamp"; exit 1; }
  test -f "$APP_DIR/tests/fixtures/irs_bmf_smoke.csv" || { echo "fixture missing"; exit 1; }
  cd "$APP_DIR" && \
  doppler run -- bash -c '"'"'
    set -e
    PYTHONPATH=. python3 scripts/run_irs_bmf_ingest.py --fixture tests/fixtures/irs_bmf_smoke.csv
    N1=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM entities.source_irs_bmf
        WHERE source_filename = '"'"'"'"'"'"'"'"'irs_bmf_smoke.csv'"'"'"'"'"'"'"'"';
    ")
    PYTHONPATH=. python3 scripts/run_irs_bmf_ingest.py --fixture tests/fixtures/irs_bmf_smoke.csv
    N2=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM entities.source_irs_bmf
        WHERE source_filename = '"'"'"'"'"'"'"'"'irs_bmf_smoke.csv'"'"'"'"'"'"'"'"';
    ")
    [ "$N1" = "2" ] && [ "$N2" = "2" ] || { echo "idempotency failed: N1=$N1 N2=$N2"; exit 1; }
    echo "s3 ok"
  '"'"'
'

# ---------------------------------------------------------------------------- #
# s4: code — scripts/run_propublica_nonprofit_ingest.py
# Conflict key is (ein, tax_prd, formtype). Provenance is propublica_nonprofit_explorer.
# Fixture: apps/data-engine-x/tests/fixtures/propublica_nonprofit_smoke.json
#   (ProPublica API returns JSON, not CSV — fixture mirrors the per-org API response).
# ---------------------------------------------------------------------------- #
S4_CHECK='
  test -f "$APP_DIR/scripts/run_propublica_nonprofit_ingest.py" || { echo "script missing"; exit 1; }
  python3 -c "import ast; ast.parse(open('"'"'$APP_DIR/scripts/run_propublica_nonprofit_ingest.py'"'"').read())" || { echo "syntax error"; exit 1; }
  grep -q "ON CONFLICT (ein, tax_prd, formtype)" "$APP_DIR/scripts/run_propublica_nonprofit_ingest.py" || { echo "missing composite ON CONFLICT"; exit 1; }
  grep -q "raw_source_row" "$APP_DIR/scripts/run_propublica_nonprofit_ingest.py" || { echo "missing raw_source_row"; exit 1; }
  grep -q "propublica_nonprofit_explorer" "$APP_DIR/scripts/run_propublica_nonprofit_ingest.py" || { echo "missing source_provider stamp"; exit 1; }
  test -f "$APP_DIR/tests/fixtures/propublica_nonprofit_smoke.json" || { echo "fixture missing"; exit 1; }
  cd "$APP_DIR" && \
  doppler run -- bash -c '"'"'
    set -e
    PYTHONPATH=. python3 scripts/run_propublica_nonprofit_ingest.py --fixture tests/fixtures/propublica_nonprofit_smoke.json
    N1=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM entities.source_propublica_nonprofits
        WHERE source_filename = '"'"'"'"'"'"'"'"'propublica_nonprofit_smoke.json'"'"'"'"'"'"'"'"';
    ")
    PYTHONPATH=. python3 scripts/run_propublica_nonprofit_ingest.py --fixture tests/fixtures/propublica_nonprofit_smoke.json
    N2=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM entities.source_propublica_nonprofits
        WHERE source_filename = '"'"'"'"'"'"'"'"'propublica_nonprofit_smoke.json'"'"'"'"'"'"'"'"';
    ")
    [ "$N1" = "2" ] && [ "$N2" = "2" ] || { echo "idempotency failed: N1=$N1 N2=$N2"; exit 1; }
    echo "s4 ok"
  '"'"'
'

run_surface "s1" "bencrane/hq-all" "$S1_CHECK"
run_surface "s2" "bencrane/hq-all" "$S2_CHECK"
run_surface "s3" "bencrane/hq-all" "$S3_CHECK"
run_surface "s4" "bencrane/hq-all" "$S4_CHECK"

echo "==> All filtered surfaces passed."
