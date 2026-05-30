#!/usr/bin/env bash
# Verification harness for /scope cycle hq-command-data-view.
#
# Runs all F2P (FAIL_TO_PASS) and P2P (PASS_TO_PASS) checks per the frozen
# contract at ~/Desktop/hq/scope-status/hq-command-data-view/contract.md.
#
# Accepts:
#   --surface <id>    run a single surface (e.g. --surface f2p-3)
#   --repo <name>     filter to one repo (only hq-all in this cycle)
#
# Modeled after hq-all-sba-7a-essentials-lance-emit.sh (commit db5ad93a).
# Sources _lib-shim.sh for dex_psql_query and Doppler-wrapped helpers.
#
# Validator-frozen reference numbers (measured 2026-05-14):
#   F2P-3  COMMIT total_count             = 23,773   (± 2% tolerance)
#   F2P-4  COMMIT + 2026-01-01 since      = 10,874   (± 2% tolerance)
#   F2P-5  CSV line count (header + rows) = 23,774   (± 2% tolerance)
#   F2P-1  active Lance datasets floor    = 44       (observed 45; floor = obs − 1)
#   F2P-2  Lance schema column count       ≥ 45      (observed 47)
#
# TODO(executor): before invoking this harness, start BOTH dev servers:
#   1. data-engine-x (uvicorn on :8080)
#        cd apps/data-engine-x && \
#          doppler run --project hq-all --config prd -- \
#          uv run uvicorn app.main:app --port 8080 &
#   2. hq-command (Next.js dev on :3000) — ONLY if any UI assertion needs a
#      running app. The current F2P-7..F2P-12 checks use static module imports
#      under vitest and do NOT require the dev server to be running.
#
# The harness asserts both pre-flight reachability checks before running F2P.

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# Prefer ~/hq-all (the current monorepo checkout); fall back to ~/Desktop/hq-all
# (legacy auxiliary clone, often stale).
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

# Local dev-server endpoints (executor starts these before invoking).
DEX_BASE="${DEX_BASE:-http://localhost:8080}"
HQC_BASE="${HQC_BASE:-http://localhost:3000}"

# Acceptance dataset (the SBA 7(a) Lance browser is the F2P proof-cohort).
SLUG="sba_7a_loans_essentials_lance"

# Frozen reference numbers (validator 2026-05-14).
EXPECTED_COMMIT=23773
EXPECTED_SINCE_2026=10874
EXPECTED_CSV_LINES=23774
EXPECTED_LANCE_FLOOR=44
EXPECTED_COL_FLOOR=45

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

# --- ± tolerance helper -------------------------------------------------- #
# _within_tolerance <actual> <expected> <pct>   exit 0 iff |a-e|/e < pct/100
_within_tolerance() {
  local actual="$1" expected="$2" pct="$3"
  if ! [[ "$actual" =~ ^[0-9]+$ ]]; then
    echo "FAIL: actual=$actual is not a number" >&2
    return 1
  fi
  python3 -c "
import sys
a, e, p = int('$actual'), int('$expected'), float('$pct')
delta_pct = abs(a - e) / e * 100
if delta_pct < p:
    print(f'PASS: actual={a:,} expected={e:,} delta={delta_pct:.2f}% < {p}%')
    sys.exit(0)
print(f'FAIL: actual={a:,} expected={e:,} delta={delta_pct:.2f}% >= {p}%')
sys.exit(1)
"
}

# --- Pre-flight: backend dev server reachable ---------------------------- #
_dex_alive() {
  curl --silent --fail --max-time 2 "$DEX_BASE/health" > /dev/null 2>&1 || {
    echo "FAIL: $DEX_BASE/health unreachable. Start uvicorn first:" >&2
    echo "  cd apps/data-engine-x && doppler run --project hq-all --config prd -- uv run uvicorn app.main:app --port 8080" >&2
    return 1
  }
  return 0
}

# ── F2P-1: list datasets (>=44 active Lance rows in the response) ──────── #
_f2p1_check() {
  _dex_alive || return 1
  local resp len
  resp=$(curl --silent --fail "$DEX_BASE/api/internal/catalog/datasets") || {
    echo "FAIL: GET /api/internal/catalog/datasets did not return 2xx" >&2
    return 1
  }
  # Envelope shape: { "data": { "datasets": [ ... ] } } OR { "data": [...] }
  len=$(echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin)
d = r.get('data', r)
if isinstance(d, dict) and 'datasets' in d:
    items = d['datasets']
elif isinstance(d, list):
    items = d
else:
    items = []
print(len(items))
# Also assert required fields present on first item
if items:
    first = items[0]
    required = {'slug', 'display_name', 'kind', 'lance_uri'}
    present = required & set(first.keys())
    if present != required:
        missing = required - present
        print(f'MISSING_FIELDS: {missing}', file=sys.stderr)
        sys.exit(1)
") || return 1
  if (( len < EXPECTED_LANCE_FLOOR )); then
    echo "FAIL: only $len datasets in response, floor is $EXPECTED_LANCE_FLOOR" >&2
    return 1
  fi
  echo "PASS: $len datasets (>= $EXPECTED_LANCE_FLOOR), required fields present"
}
run_surface "f2p-1" "hq-all" '_f2p1_check'

# ── F2P-2: schema reflection (>=45 cols, required cols present) ───────── #
_f2p2_check() {
  _dex_alive || return 1
  local resp
  resp=$(curl --silent --fail "$DEX_BASE/api/internal/catalog/datasets/$SLUG/schema") || {
    echo "FAIL: GET schema endpoint did not return 2xx" >&2
    return 1
  }
  echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin)
d = r.get('data', r)
cols = d.get('columns') if isinstance(d, dict) else d
if not isinstance(cols, list):
    print(f'FAIL: columns is not a list; got {type(cols).__name__}', file=sys.stderr)
    sys.exit(1)
names = {c.get('name') for c in cols}
required = {'approvaldate', 'firstdisbursementdate', 'loanstatus', 'borrname'}
missing = required - names
if missing:
    print(f'FAIL: missing required columns: {missing}', file=sys.stderr)
    sys.exit(1)
if len(cols) < $EXPECTED_COL_FLOOR:
    print(f'FAIL: only {len(cols)} columns; floor is $EXPECTED_COL_FLOOR', file=sys.stderr)
    sys.exit(1)
# Also verify each column has an arrow_type field
no_type = [c.get('name') for c in cols if 'arrow_type' not in c]
if no_type:
    print(f'FAIL: columns missing arrow_type: {no_type[:5]}', file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(cols)} columns, all required present, arrow_type set')
"
}
run_surface "f2p-2" "hq-all" '_f2p2_check'

# ── F2P-3: slice POST with COMMIT filter → total_count ≈ 23,773 ────────── #
_f2p3_check() {
  _dex_alive || return 1
  local resp body
  body='{"filters":[{"col":"loanstatus","op":"eq","value":"COMMIT"}],"limit":50,"offset":0}'
  resp=$(curl --silent --fail -X POST -H 'Content-Type: application/json' \
    -d "$body" "$DEX_BASE/api/internal/catalog/datasets/$SLUG/slice") || {
    echo "FAIL: POST slice did not return 2xx" >&2
    return 1
  }
  echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin)
d = r.get('data', r)
rows = d.get('rows', [])
total = d.get('total_count', -1)
schema = d.get('schema', [])
print(f'rows_len={len(rows)} total_count={total} schema_len={len(schema)}')
if len(rows) != 50:
    print(f'FAIL: rows_len {len(rows)} != 50', file=sys.stderr); sys.exit(1)
if not isinstance(total, int):
    print(f'FAIL: total_count {total!r} is not int', file=sys.stderr); sys.exit(1)
if not schema:
    print('FAIL: schema empty', file=sys.stderr); sys.exit(1)
print(total)
" > /tmp/hq_f2p3_total.txt || return 1
  local actual
  actual=$(tail -n1 /tmp/hq_f2p3_total.txt)
  _within_tolerance "$actual" "$EXPECTED_COMMIT" 2
}
run_surface "f2p-3" "hq-all" '_f2p3_check'

# ── F2P-4: slice with COMMIT + date filter → total_count ≈ 10,874 ──────── #
_f2p4_check() {
  _dex_alive || return 1
  local resp body
  body='{"filters":[{"col":"loanstatus","op":"eq","value":"COMMIT"},{"col":"approvaldate","op":"gte","value":"2026-01-01"}],"limit":50,"offset":0}'
  resp=$(curl --silent --fail -X POST -H 'Content-Type: application/json' \
    -d "$body" "$DEX_BASE/api/internal/catalog/datasets/$SLUG/slice") || {
    echo "FAIL: POST slice (date filter) did not return 2xx" >&2
    return 1
  }
  local actual
  actual=$(echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin); d = r.get('data', r)
print(d.get('total_count', -1))
") || return 1
  _within_tolerance "$actual" "$EXPECTED_SINCE_2026" 2
}
run_surface "f2p-4" "hq-all" '_f2p4_check'

# ── F2P-5: CSV export streams with line count ≈ 23,774 ─────────────────── #
_f2p5_check() {
  _dex_alive || return 1
  local tmpfile filters_json filters_url first_line lines
  tmpfile="/tmp/hq_f2p5_export_$$.csv"
  # filters payload (same as F2P-3: COMMIT only)
  filters_json='[{"col":"loanstatus","op":"eq","value":"COMMIT"}]'
  filters_url=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$filters_json")
  curl --silent --fail --output "$tmpfile" \
    "$DEX_BASE/api/internal/catalog/datasets/$SLUG/export.csv?filters=$filters_url" || {
    echo "FAIL: GET export.csv did not return 2xx" >&2
    return 1
  }
  lines=$(wc -l < "$tmpfile" | tr -d ' ')
  first_line=$(head -n1 "$tmpfile")
  # Header sanity: contains commas + 'loanstatus' (one of the asserted columns)
  if ! echo "$first_line" | grep -q ','; then
    echo "FAIL: first line of CSV has no commas: $first_line" >&2
    rm -f "$tmpfile"
    return 1
  fi
  if ! echo "$first_line" | grep -q 'loanstatus'; then
    echo "FAIL: first line does not contain 'loanstatus': $first_line" >&2
    rm -f "$tmpfile"
    return 1
  fi
  rm -f "$tmpfile"
  _within_tolerance "$lines" "$EXPECTED_CSV_LINES" 2
}
run_surface "f2p-5" "hq-all" '_f2p5_check'

# ── F2P-6: health endpoint surfaces divergences ────────────────────────── #
_f2p6_check() {
  _dex_alive || return 1
  local resp
  resp=$(curl --silent --fail "$DEX_BASE/api/internal/catalog/health") || {
    echo "FAIL: GET /api/internal/catalog/health did not return 2xx" >&2
    return 1
  }
  echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin); d = r.get('data', r)
required_top = {'registered_total', 'polaris_total', 'r2_prefix_total', 'divergences'}
missing = required_top - set(d.keys())
if missing:
    print(f'FAIL: missing top-level fields: {missing}', file=sys.stderr); sys.exit(1)
div = d.get('divergences', [])
if not isinstance(div, list):
    print('FAIL: divergences is not a list', file=sys.stderr); sys.exit(1)
valid_kinds = {'r2_prefix_no_emit', 'lance_uri_empty_or_stale', 'polaris_not_registered', 'daily_script_stale'}
seen_kinds = set()
for entry in div:
    if not isinstance(entry, dict):
        continue
    if 'kind' not in entry or 'target' not in entry:
        print(f'FAIL: divergence missing kind/target: {entry}', file=sys.stderr); sys.exit(1)
    if entry['kind'] in valid_kinds:
        seen_kinds.add(entry['kind'])
if not seen_kinds:
    print(f'FAIL: zero divergences of any valid kind {valid_kinds}', file=sys.stderr); sys.exit(1)
print(f'PASS: registered={d[\"registered_total\"]} polaris={d[\"polaris_total\"]} r2={d[\"r2_prefix_total\"]} divergence_kinds={sorted(seen_kinds)}')
"
}
run_surface "f2p-6" "hq-all" '_f2p6_check'

# ── F2P-7: /admin tile linking to /admin/data-view ─────────────────────── #
# Static source check on the TILES array — vitest-equivalent.
_f2p7_check() {
  local page="$HQ_ALL_ROOT/apps/hq-command/app/admin/page.tsx"
  test -f "$page" || { echo "FAIL: missing $page" >&2; return 1; }
  grep -q "href: '/admin/data-view'" "$page" || {
    echo "FAIL: /admin/page.tsx TILES does not contain href: '/admin/data-view'" >&2
    return 1
  }
  # Also: the title near that href matches /Data View/i
  python3 - <<'PY' "$page" || return 1
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"href:\s*'/admin/data-view'[\s\S]{0,300}title:\s*'([^']+)'", src)
if not m:
    print("FAIL: could not parse title adjacent to /admin/data-view href", file=sys.stderr)
    sys.exit(1)
title = m.group(1)
if not re.search(r"data view", title, re.IGNORECASE):
    print(f"FAIL: title {title!r} does not match /Data View/i", file=sys.stderr)
    sys.exit(1)
print(f"PASS: tile present with title={title!r}")
PY
}
run_surface "f2p-7" "hq-all" '_f2p7_check'

# ── F2P-8: /admin Data Health entry point ──────────────────────────────── #
# Accepts EITHER (a) a new tile linking to /admin/data-health OR (b) the
# existing /admin/data-ingests tile annotated/enhanced to surface the four
# divergence kinds (proxy: grep for at least 2 divergence-kind tokens in
# the description string for that tile).
_f2p8_check() {
  local page="$HQ_ALL_ROOT/apps/hq-command/app/admin/page.tsx"
  test -f "$page" || { echo "FAIL: missing $page" >&2; return 1; }
  if grep -q "href: '/admin/data-health'" "$page"; then
    echo "PASS: new tile /admin/data-health present"
    return 0
  fi
  # Else: data-ingests tile must mention divergence kinds
  python3 - <<'PY' "$page" || return 1
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"href:\s*'/admin/data-ingests'[\s\S]{0,500}description:\s*'([^']+)'", src)
if not m:
    print("FAIL: neither /admin/data-health tile nor enhanced /admin/data-ingests tile found", file=sys.stderr)
    sys.exit(1)
desc = m.group(1).lower()
tokens = ['r2', 'lance', 'polaris', 'daily-script', 'divergen', 'health', 'gap']
hits = [t for t in tokens if t in desc]
if len(hits) < 2:
    print(f"FAIL: /admin/data-ingests description has <2 divergence-kind tokens: hits={hits}", file=sys.stderr)
    sys.exit(1)
print(f"PASS: /admin/data-ingests tile enhanced (tokens={hits})")
PY
}
run_surface "f2p-8" "hq-all" '_f2p8_check'

# ── F2P-9: Data View page (picker + filters + table + export button) ───── #
_f2p9_check() {
  local page_dir="$HQ_ALL_ROOT/apps/hq-command/app/admin/data-view"
  test -d "$page_dir" || { echo "FAIL: missing $page_dir/" >&2; return 1; }
  # Look for the page.tsx (Next.js convention) — directly OR via a layout split
  if [[ ! -f "$page_dir/page.tsx" ]]; then
    echo "FAIL: missing $page_dir/page.tsx" >&2
    return 1
  fi
  # Search for canonical idioms across the data-view dir
  local hits_picker hits_filter hits_table hits_export
  hits_picker=$(grep -rEl 'dataset.?picker|datasetSelect|<Select|combobox' "$page_dir" "$HQ_ALL_ROOT/apps/hq-command/components" 2>/dev/null | head -5 | wc -l | tr -d ' ')
  hits_filter=$(grep -rEl 'filter.*input|FilterInput|filterPanel|onAddFilter|filters\[' "$page_dir" "$HQ_ALL_ROOT/apps/hq-command/components" 2>/dev/null | head -5 | wc -l | tr -d ' ')
  hits_table=$(grep -rEl 'preview.?table|<table|DataTable|tanstack/react-table|@tanstack/react-table' "$page_dir" "$HQ_ALL_ROOT/apps/hq-command/components" 2>/dev/null | head -5 | wc -l | tr -d ' ')
  hits_export=$(grep -rE 'export.csv|export_csv|Export CSV|exportCsv' "$page_dir" 2>/dev/null | wc -l | tr -d ' ')
  echo "data-view UI hits: picker=$hits_picker filter=$hits_filter table=$hits_table export=$hits_export"
  if (( hits_picker < 1 || hits_filter < 1 || hits_table < 1 || hits_export < 1 )); then
    echo "FAIL: data-view page missing UI idiom (need picker, filter, table, export)" >&2
    return 1
  fi
  echo "PASS: data-view page assembles required UI elements"
}
run_surface "f2p-9" "hq-all" '_f2p9_check'

# ── F2P-10: UI count badge (vitest unit) — defer to test runner ────────── #
# The harness runs vitest on the hq-command test that exercises the count
# badge: filters={loanstatus:'COMMIT'}, dataset=sba_7a_loans_essentials_lance,
# asserts a rendered count = 23,773 ± 2%. The test mocks fetch to return
# the canonical response shape (the F2P-3 server is NOT required to be
# running for the unit test). Test path: apps/hq-command/tests/data-view-count-badge.test.ts
_f2p10_check() {
  local test_path="$HQ_ALL_ROOT/apps/hq-command/tests/data-view-count-badge.test.ts"
  test -f "$test_path" || { echo "FAIL: missing test $test_path" >&2; return 1; }
  ( cd "$HQ_ALL_ROOT/apps/hq-command" && pnpm exec vitest run tests/data-view-count-badge.test.ts ) || {
    echo "FAIL: vitest data-view-count-badge.test.ts failed" >&2
    return 1
  }
  echo "PASS: data-view-count-badge vitest green"
}
run_surface "f2p-10" "hq-all" '_f2p10_check'

# ── F2P-11: CSV download trigger (vitest unit) ─────────────────────────── #
# Asserts Export CSV button click invokes the right /api/dex/api/internal/...
# /export.csv URL with the filters set. Test path:
# apps/hq-command/tests/data-view-export-csv.test.ts
_f2p11_check() {
  local test_path="$HQ_ALL_ROOT/apps/hq-command/tests/data-view-export-csv.test.ts"
  test -f "$test_path" || { echo "FAIL: missing test $test_path" >&2; return 1; }
  ( cd "$HQ_ALL_ROOT/apps/hq-command" && pnpm exec vitest run tests/data-view-export-csv.test.ts ) || {
    echo "FAIL: vitest data-view-export-csv.test.ts failed" >&2
    return 1
  }
  echo "PASS: data-view-export-csv vitest green"
}
run_surface "f2p-11" "hq-all" '_f2p11_check'

# ── F2P-12: Health dashboard renders divergence sections ───────────────── #
# Test path: apps/hq-command/tests/data-health-dashboard.test.ts — or, if the
# executor chose to enhance /admin/data-ingests, a test that asserts the
# divergence-section render on the existing page.
_f2p12_check() {
  local t1="$HQ_ALL_ROOT/apps/hq-command/tests/data-health-dashboard.test.ts"
  local t2="$HQ_ALL_ROOT/apps/hq-command/tests/data-ingests-divergences.test.ts"
  local target=""
  if [[ -f "$t1" ]]; then target="$t1"
  elif [[ -f "$t2" ]]; then target="$t2"
  else
    echo "FAIL: missing test (need $t1 OR $t2)" >&2
    return 1
  fi
  ( cd "$HQ_ALL_ROOT/apps/hq-command" && pnpm exec vitest run "tests/$(basename "$target")" ) || {
    echo "FAIL: vitest $target failed" >&2
    return 1
  }
  echo "PASS: data-health vitest green ($(basename "$target"))"
}
run_surface "f2p-12" "hq-all" '_f2p12_check'

# ── P2P-1: hq-command typecheck still green ────────────────────────────── #
_p2p1_check() {
  ( cd "$HQ_ALL_ROOT/apps/hq-command" && pnpm exec tsc --noEmit ) || {
    echo "FAIL: tsc --noEmit failed" >&2
    return 1
  }
  echo "PASS: tsc --noEmit clean"
}
run_surface "p2p-1" "hq-all" '_p2p1_check'

# ── P2P-2: hq-command lint still green ─────────────────────────────────── #
_p2p2_check() {
  ( cd "$HQ_ALL_ROOT/apps/hq-command" && pnpm -F hq lint ) || {
    echo "FAIL: pnpm lint failed" >&2
    return 1
  }
  echo "PASS: lint clean"
}
run_surface "p2p-2" "hq-all" '_p2p2_check'

# ── P2P-3: data-engine-x tests pass (no NEW failures) ──────────────────── #
# Validator records baseline pass count; executor diff must not regress.
# Conservative form: assert pytest exits 0 (allowing pre-existing failures
# expressed via expected-fail / skip markers is fine; new red is not).
_p2p3_check() {
  ( cd "$HQ_ALL_ROOT/apps/data-engine-x" && \
      doppler run --project hq-all --config prd -- \
      uv run pytest tests/ -q ) || {
    echo "FAIL: pytest non-zero" >&2
    return 1
  }
  echo "PASS: pytest green"
}
run_surface "p2p-3" "hq-all" '_p2p3_check'

# ── P2P-4: existing /admin/data-ingests Route still 200 ────────────────── #
# Static check: page file exists and exports a default function. The
# DataIngestsTable component still imports cleanly (tsc covers the
# deep type-check; this assertion catches an accidental delete).
_p2p4_check() {
  local page="$HQ_ALL_ROOT/apps/hq-command/app/admin/data-ingests/page.tsx"
  test -f "$page" || { echo "FAIL: missing $page" >&2; return 1; }
  grep -q "export default function" "$page" || {
    echo "FAIL: /admin/data-ingests/page.tsx no default export" >&2
    return 1
  }
  # Component file still present (executor not allowed to delete it)
  test -f "$HQ_ALL_ROOT/apps/hq-command/components/data-ingests/data-ingests-table.tsx" || {
    echo "FAIL: data-ingests-table.tsx missing — executor deleted regression target" >&2
    return 1
  }
  echo "PASS: /admin/data-ingests untouched at file level"
}
run_surface "p2p-4" "hq-all" '_p2p4_check'

# ── P2P-5: existing data-source-catalog endpoint unchanged shape ──────── #
# Validator captured the baseline shape: response is DataEnvelope with
# data.sources[] (each row has source_slug, display_name, strategic_role,
# r2_prefix, refresh_cadence, lifecycle_stage, ...) plus data.r2_snapshot_oldest_cached_at.
# Harness asserts the executor's diff does NOT mutate the response model.
_p2p5_check() {
  _dex_alive || return 1
  # require_flexible_auth on the endpoint — pass DEX_SERVICE_TOKEN via Doppler
  # (CLAUDE.md §Doppler shell gotcha: defer $DEX_SERVICE_TOKEN expansion to
  # AFTER doppler injects by using bash -c with escaped $).
  local resp
  resp=$(doppler run --project hq-all --config prd -- bash -c \
    "curl --silent --fail -H \"Authorization: Bearer \$DEX_SERVICE_TOKEN\" \"$DEX_BASE/api/internal/data-source-catalog\"") || {
    echo "FAIL: GET /api/internal/data-source-catalog did not return 2xx" >&2
    return 1
  }
  echo "$resp" | python3 -c "
import json, sys
r = json.load(sys.stdin); d = r.get('data', r)
if not isinstance(d, dict):
    print('FAIL: data envelope not a dict', file=sys.stderr); sys.exit(1)
if 'sources' not in d or 'r2_snapshot_oldest_cached_at' not in d:
    print(f'FAIL: top-level keys missing; got {sorted(d.keys())}', file=sys.stderr); sys.exit(1)
sources = d['sources']
if not isinstance(sources, list) or not sources:
    print('FAIL: sources is empty/non-list', file=sys.stderr); sys.exit(1)
first = sources[0]
required = {'source_slug', 'display_name', 'strategic_role', 'r2_prefix',
            'refresh_cadence', 'lifecycle_stage', 'is_active',
            'freshness_state', 'freshness_reason', 'poison_files'}
missing = required - set(first.keys())
if missing:
    print(f'FAIL: source row missing fields: {missing}', file=sys.stderr); sys.exit(1)
print(f'PASS: data-source-catalog shape intact (sources={len(sources)})')
"
}
run_surface "p2p-5" "hq-all" '_p2p5_check'

# ── Out-of-scope sentinels (enforce the directive's OOS-* rules) ──────── #
# Each sentinel is a grep/path check on the executor's diff vs the cycle's
# base ref. Defaults to `origin/main` (freshly fetched). The repo to diff is
# the EXECUTOR's worktree (auto-detected via `git rev-parse --show-toplevel`
# from the harness's actual location), NOT $HQ_ALL_ROOT — because the
# canonical $HOME/Desktop/hq-all checkout may be lagging behind the
# executor's branch.
#
# Override via env: SCOPE_BASE_REF=<ref> (e.g. SCOPE_BASE_REF=origin/main).
_oos_check() {
  # Auto-detect the executor's repo root from the path of THIS script
  local script_path script_dir repo
  script_path="${BASH_SOURCE[0]}"
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"
  repo="$(cd "$script_dir" && git rev-parse --show-toplevel 2>/dev/null)"
  if [[ -z "$repo" ]]; then
    echo "OOS FAIL: cannot resolve executor's repo root from $script_dir" >&2
    return 1
  fi
  echo "  diff-base: ${SCOPE_BASE_REF:-origin/main}  repo: $repo"

  ( cd "$repo" && {
    local base="${SCOPE_BASE_REF:-origin/main}"
    # Best-effort fetch (no-op if offline). Tolerate failure so the gate
    # still runs against the local main snapshot.
    git fetch origin main --quiet 2>/dev/null || true
    # Validate that the base ref resolves; if not, fall back to local main.
    if ! git rev-parse --verify "$base" >/dev/null 2>&1; then
      echo "  warning: $base unresolvable; falling back to local main" >&2
      base="main"
    fi

    local fail=0
    # OOS-3: no INSERT/UPDATE/DELETE on ops.data_sources
    if git diff "$base"..HEAD -- '*.py' '*.sql' 2>/dev/null | grep -qiE 'INSERT INTO ops\.data_sources|UPDATE ops\.data_sources|DELETE FROM ops\.data_sources'; then
      echo "OOS-3 FAIL: diff writes to ops.data_sources" >&2; fail=1
    fi
    # OOS-4: no persistent cache layer references
    if git diff "$base"..HEAD 2>/dev/null | grep -qiE '\bredis\b|memcached|cdn-cache|cloudflare-kv|durable.?object'; then
      echo "OOS-4 FAIL: persistent-cache identifier in diff" >&2; fail=1
    fi
    # OOS-5: no NEW Lance emit scripts
    if git diff --name-status "$base"..HEAD 2>/dev/null | grep -qE '^A\s+apps/data-engine-x/scripts/emit_.*_lance\.py$'; then
      echo "OOS-5 FAIL: new emit_*_lance.py script added" >&2; fail=1
    fi
    # OOS-6: no NEW supabase migrations
    if git diff --name-status "$base"..HEAD 2>/dev/null | grep -qE '^A\s+apps/data-engine-x/supabase/migrations/'; then
      echo "OOS-6 FAIL: new supabase migration added" >&2; fail=1
    fi
    # OOS-7: no auth/ touches
    if git diff --name-only "$base"..HEAD 2>/dev/null | grep -qE '^apps/data-engine-x/app/auth/'; then
      echo "OOS-7 FAIL: app/auth/ modified" >&2; fail=1
    fi
    # OOS-8: no risingwave/supabase-migrations/modal touches
    if git diff --name-only "$base"..HEAD 2>/dev/null | grep -qE '^apps/data-engine-x/(risingwave|supabase/migrations|modal)/'; then
      echo "OOS-8 FAIL: forbidden DEX subdir touched" >&2; fail=1
    fi
    # OOS-9: no hq-x/partner-platform/managed-agents-x touches
    if git diff --name-only "$base"..HEAD 2>/dev/null | grep -qE '^apps/(hq-x|partner-platform|managed-agents-x|managed-agents)/'; then
      echo "OOS-9 FAIL: forbidden sibling app touched" >&2; fail=1
    fi
    # OOS-10: no auth-proxy boundary touches
    if git diff --name-only "$base"..HEAD 2>/dev/null | grep -qE 'apps/hq-command/(middleware\.ts|app/api/dex/\[\.\.\.path\]/route\.ts|lib/proxy/server\.ts|lib/supabase/)'; then
      echo "OOS-10 FAIL: hq-command auth-proxy boundary touched" >&2; fail=1
    fi
    if (( fail == 0 )); then
      echo "PASS: all OOS sentinels clean (base=$base)"
      exit 0
    else
      exit 1
    fi
  } )
}
run_surface "oos" "hq-all" '_oos_check'

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
