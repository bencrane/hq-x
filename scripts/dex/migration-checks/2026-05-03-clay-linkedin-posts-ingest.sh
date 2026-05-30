#!/usr/bin/env bash
# Verification harness for directive: 2026-05-03-clay-linkedin-posts-ingest
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (4):
#   s1  migration  apps/data-engine-x/supabase/migrations/20260503190000_source_linkedin_posts.sql
#                  (creates entities.source_linkedin_posts + ops.linkedin_posts_ingest_runs)
#   s2  code       apps/data-engine-x/app/services/clay_linkedin_posts.py (NEW)
#                  ingest_clay_linkedin_posts(*, source_table, posts) -> {action,run_id,posts_upserted}
#   s3  code       apps/data-engine-x/modal/dex_ingest_app.py (EXISTING — additive)
#                  Adds ClayLinkedinPostsIngestRequest model + POST /clay-linkedin-posts route.
#   s4  code       apps/data-engine-x/tests/test_clay_linkedin_posts.py (NEW — flat layout)
#                  Four tests: extract_post_urn happy/sad, round-trip via supabase mock,
#                  empty-payload raises.
#
# Migration timestamp note: validator originally chose `20260503180000`, but PR #24 (CMS
# doctors-clinicians) merged before audit and consumed that slot on main
# (`20260503180000_doctors_clinicians_source_tables.sql`). Audit re-checked main and
# moved the chosen slot forward to `20260503190000`.
#
# Test path note: validator corrected the directive's `tests/services/test_clay_linkedin_posts.py`
# to the flat layout `tests/test_clay_linkedin_posts.py`. The s4 verify command in this
# harness uses the corrected path.
#
# Doppler idiom (apps/data-engine-x/CLAUDE.md §"Doppler Shell Gotcha"):
#   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -c "..."'
# DDL connection MUST use DEX_DB_URL_DIRECT (pgbouncer transaction-mode can't run DDL).
# Doppler project is invoked explicitly as `--project data-engine-x --config prd` to bypass
# the per-worktree pin (matches IRS BMF precedent).
#
# Usage: ./2026-05-03-clay-linkedin-posts-ingest.sh \
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

echo "==> Verifying clay-linkedin-posts-ingest surfaces (filter: ${REPO_FILTER:-all})"

# ---------------------------------------------------------------------------- #
# s1: migration — entities.source_linkedin_posts + ops.linkedin_posts_ingest_runs
# Asserts: source table exists, raw_source_row column present, PK = post_urn,
#          ops.linkedin_posts_ingest_runs sibling exists, all four required
#          indexes present.
# ---------------------------------------------------------------------------- #
S1_CHECK='
  doppler run --project data-engine-x --config prd -- bash -c '"'"'
    set -e
    EXISTS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.tables
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_linkedin_posts'"'"'"'"'"'"'"'"';
    ")
    [ "$EXISTS" = "1" ] || { echo "table entities.source_linkedin_posts missing"; exit 1; }
    HAS_RAW=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT 1 FROM information_schema.columns
        WHERE table_schema = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND table_name   = '"'"'"'"'"'"'"'"'source_linkedin_posts'"'"'"'"'"'"'"'"'
          AND column_name  = '"'"'"'"'"'"'"'"'raw_source_row'"'"'"'"'"'"'"'"'
          AND is_nullable  = '"'"'"'"'"'"'"'"'NO'"'"'"'"'"'"'"'"';
    ")
    [ "$HAS_RAW" = "1" ] || { echo "raw_source_row missing or nullable"; exit 1; }
    PK=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = '"'"'"'"'"'"'"'"'entities.source_linkedin_posts'"'"'"'"'"'"'"'"'::regclass
          AND i.indisprimary;
    ")
    [ "$PK" = "post_urn" ] || { echo "expected PK post_urn, got $PK"; exit 1; }
    RUNS=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT to_regclass('"'"'"'"'"'"'"'"'ops.linkedin_posts_ingest_runs'"'"'"'"'"'"'"'"') IS NOT NULL;
    ")
    [ "$RUNS" = "t" ] || { echo "ops.linkedin_posts_ingest_runs missing"; exit 1; }
    IDX=$(psql "$DEX_DB_URL_DIRECT" -tAX -v ON_ERROR_STOP=1 -c "
      SELECT count(*) FROM pg_indexes
        WHERE schemaname = '"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"'
          AND tablename  = '"'"'"'"'"'"'"'"'source_linkedin_posts'"'"'"'"'"'"'"'"'
          AND indexname IN (
            '"'"'"'"'"'"'"'"'idx_source_linkedin_posts_author_url'"'"'"'"'"'"'"'"',
            '"'"'"'"'"'"'"'"'idx_source_linkedin_posts_created_at'"'"'"'"'"'"'"'"',
            '"'"'"'"'"'"'"'"'idx_source_linkedin_posts_mentions_gin'"'"'"'"'"'"'"'"',
            '"'"'"'"'"'"'"'"'idx_source_linkedin_posts_ingested_at'"'"'"'"'"'"'"'"'
          );
    ")
    [ "$IDX" = "4" ] || { echo "expected 4 indexes, got $IDX"; exit 1; }
    echo "s1 ok"
  '"'"'
'

# ---------------------------------------------------------------------------- #
# s2: code — app/services/clay_linkedin_posts.py
# Asserts: file exists, parses, has the public function, hits on_conflict on
# post_urn, references raw_source_row, references ops.linkedin_posts_ingest_runs
# bookkeeping (look for the 'running'/'succeeded'/'failed' lifecycle strings).
# ---------------------------------------------------------------------------- #
S2_CHECK='
  cd /Users/benjamincrane/hq-all/apps/data-engine-x && \
  test -f app/services/clay_linkedin_posts.py || { echo "service file missing"; exit 1; }
  python3 -c "import ast; ast.parse(open('"'"'app/services/clay_linkedin_posts.py'"'"').read())" || { echo "syntax error"; exit 1; }
  grep -q "def ingest_clay_linkedin_posts" app/services/clay_linkedin_posts.py || { echo "missing ingest_clay_linkedin_posts"; exit 1; }
  grep -q "on_conflict=\"post_urn\"" app/services/clay_linkedin_posts.py || { echo "missing on_conflict=post_urn"; exit 1; }
  grep -q "raw_source_row" app/services/clay_linkedin_posts.py || { echo "missing raw_source_row"; exit 1; }
  grep -q "linkedin_posts_ingest_runs" app/services/clay_linkedin_posts.py || { echo "missing run-row table reference"; exit 1; }
  grep -q "succeeded" app/services/clay_linkedin_posts.py || { echo "missing succeeded status"; exit 1; }
  grep -q "failed" app/services/clay_linkedin_posts.py || { echo "missing failed status"; exit 1; }
  grep -q "urn:li:activity" app/services/clay_linkedin_posts.py || { echo "missing URN extraction regex anchor"; exit 1; }
  echo "s2 ok"
'

# ---------------------------------------------------------------------------- #
# s3: code — modal/dex_ingest_app.py additive change
# Asserts: file parses, new model + route are present, route is registered on web_app.
# Static introspection only — no live Modal runtime needed. The dynamic route check
# imports modal/ as a package; modal is a real installed dep so the import succeeds.
# ---------------------------------------------------------------------------- #
S3_CHECK='
  cd /Users/benjamincrane/hq-all/apps/data-engine-x && \
  python3 -c "import ast; ast.parse(open('"'"'modal/dex_ingest_app.py'"'"').read())" || { echo "syntax error"; exit 1; }
  grep -q "ClayLinkedinPostsIngestRequest" modal/dex_ingest_app.py || { echo "missing model class"; exit 1; }
  grep -q "/clay-linkedin-posts" modal/dex_ingest_app.py || { echo "missing route path"; exit 1; }
  grep -q "from app.services.clay_linkedin_posts import ingest_clay_linkedin_posts" modal/dex_ingest_app.py || { echo "missing lazy import of service"; exit 1; }
  PYTHONPATH=. python3 -c "
import sys; sys.path.insert(0, '"'"'.'"'"')
from modal.dex_ingest_app import web_app
paths = sorted(getattr(r, '"'"'path'"'"', None) for r in web_app.routes if getattr(r, '"'"'path'"'"', None))
assert '"'"'/clay-linkedin-posts'"'"' in paths, f'"'"'route not registered, got {paths}'"'"'
print('"'"'s3 ok'"'"')
"
'

# ---------------------------------------------------------------------------- #
# s4: code — tests/test_clay_linkedin_posts.py (FLAT layout — see note above)
# Asserts: file exists, all 4 test functions present by name, pytest run passes.
# ---------------------------------------------------------------------------- #
S4_CHECK='
  cd /Users/benjamincrane/hq-all/apps/data-engine-x && \
  test -f tests/test_clay_linkedin_posts.py || { echo "test file missing"; exit 1; }
  python3 -c "import ast; ast.parse(open('"'"'tests/test_clay_linkedin_posts.py'"'"').read())" || { echo "syntax error"; exit 1; }
  grep -q "def test_extract_post_urn_happy_path" tests/test_clay_linkedin_posts.py || { echo "missing test_extract_post_urn_happy_path"; exit 1; }
  grep -q "def test_extract_post_urn_raises_on_bad_url" tests/test_clay_linkedin_posts.py || { echo "missing test_extract_post_urn_raises_on_bad_url"; exit 1; }
  grep -q "def test_ingest_clay_linkedin_posts_round_trip" tests/test_clay_linkedin_posts.py || { echo "missing test_ingest_clay_linkedin_posts_round_trip"; exit 1; }
  grep -q "def test_ingest_clay_linkedin_posts_empty_payload_raises" tests/test_clay_linkedin_posts.py || { echo "missing test_ingest_clay_linkedin_posts_empty_payload_raises"; exit 1; }
  python3 -m pytest tests/test_clay_linkedin_posts.py -v
'

run_surface "s1" "bencrane/hq-all" "$S1_CHECK"
run_surface "s2" "bencrane/hq-all" "$S2_CHECK"
run_surface "s3" "bencrane/hq-all" "$S3_CHECK"
run_surface "s4" "bencrane/hq-all" "$S4_CHECK"

echo "==> All filtered surfaces passed."
