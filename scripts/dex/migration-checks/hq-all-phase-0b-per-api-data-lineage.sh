#!/usr/bin/env bash
# Verification harness for hq-all Phase 0b: Per-API Data Lineage
#
# Asserts that every HTTP response from DEX (api.dataengine.run) and hq-x
# (api.opsengine.run) carries an X-Data-Lineage header (JSON-encoded array
# of {table, snapshot_id, format, queried_at} entries) listing the catalog
# tables read to compute the response.
#
# Verification gate (load-bearing):
#   - 5 representative endpoints (3 DEX, 2 hq-x) — at-least-one DEX and
#     at-least-one hq-x with non-empty lineage; all 5 with header present
#     + JSON-decodable.
#   - /health on both DEX and hq-x returns explicit empty array `[]` (not
#     missing).
#   - Phase 0a sources endpoint returns 3 ops.* Postgres entries.
#   - hq-x proxied response merges DEX-side lineage entries.
#
# Exits 0 only if ALL checks pass. Sources Doppler env via the standard
# data-engine-x helper library.
#
# Usage:
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-phase-0b-per-api-data-lineage.sh
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-phase-0b-per-api-data-lineage.sh --repo hq-all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_lib-shim.sh"

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *)      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "hq-all" ]]; then
  echo "harness: repo filter $REPO_FILTER does not match hq-all — nothing to do." >&2
  exit 0
fi

DEX_BASE="${DEX_PHASE_0B_BASE_URL:-https://api.dataengine.run}"
HQX_BASE="${HQX_PHASE_0B_BASE_URL:-https://api.opsengine.run}"

# Doppler-resolved super-admin API key (cached locally to avoid 6× doppler
# round-trips for the 5-endpoint gate + /health probes). Also cache the
# TRIGGER_SHARED_SECRET — required by hq-x's flexible auth for the
# observability proxy endpoint (DEX_SERVICE_TOKEN is rejected by hq-x;
# only DEX accepts it).
DEX_API_KEY=$(doppler run --project hq-all --config prd -- printenv DEX_SERVICE_TOKEN)
HQX_TRIGGER_SECRET=$(doppler run --project hq-all --config prd -- printenv TRIGGER_SHARED_SECRET)
if [[ -z "$DEX_API_KEY" ]]; then
  echo "FAIL — DEX_SERVICE_TOKEN not in Doppler hq-all/prd" >&2
  exit 1
fi
if [[ -z "$HQX_TRIGGER_SECRET" ]]; then
  echo "FAIL — TRIGGER_SHARED_SECRET not in Doppler hq-all/prd" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Curl an endpoint, return the X-Data-Lineage header value verbatim (empty
# string if header not present). Stderr captures the HTTP status for
# diagnostics; stdout is the header value alone.
fetch_lineage_header() {
  # Args: <url> [bearer-token-override] [-X METHOD] [-d body] [-H "header: value"]...
  # Uses DEX_API_KEY by default. Pass second arg to override the bearer token
  # (e.g., HQX_TRIGGER_SECRET for hq-x endpoints).
  local url="$1"
  local bearer="${2:-$DEX_API_KEY}"
  shift 2 2>/dev/null || shift $#
  local raw_headers
  if [[ $# -gt 0 ]]; then
    raw_headers=$(curl -sS -i -o /dev/null -D - \
      -H "Authorization: Bearer $bearer" \
      "$@" \
      "$url" 2>&1) || {
        echo ""
        return 0
      }
  else
    raw_headers=$(curl -sS -i -o /dev/null -D - \
      -H "Authorization: Bearer $bearer" \
      "$url" 2>&1) || {
        echo ""
        return 0
      }
  fi
  echo "$raw_headers" | grep -iE '^x-data-lineage:' | head -n1 | sed -E 's/^[Xx]-[Dd]ata-[Ll]ineage:[[:space:]]*//' | tr -d '\r'
}

# JSON-decode the lineage payload and assert (1) it parses (2) every entry
# has the four required keys. Returns 0 on pass, 1 on fail.
validate_lineage_payload() {
  local payload="$1"
  local label="$2"
  if [[ -z "$payload" ]]; then
    echo "FAIL — $label: X-Data-Lineage header MISSING (must be present, even if empty)" >&2
    return 1
  fi
  python3 - "$payload" "$label" <<'PY' || return 1
import json, sys
payload, label = sys.argv[1], sys.argv[2]
try:
    arr = json.loads(payload)
except Exception as e:
    print(f"FAIL — {label}: lineage payload not JSON-decodable: {e!r}; payload={payload!r}", file=sys.stderr)
    sys.exit(1)
if not isinstance(arr, list):
    print(f"FAIL — {label}: lineage payload is not a list, got {type(arr).__name__}: {payload!r}", file=sys.stderr)
    sys.exit(1)
required_keys = {"table", "snapshot_id", "format", "queried_at"}
for i, entry in enumerate(arr):
    if not isinstance(entry, dict):
        print(f"FAIL — {label}: entry {i} is not a dict: {entry!r}", file=sys.stderr)
        sys.exit(1)
    missing = required_keys - set(entry)
    if missing:
        print(f"FAIL — {label}: entry {i} missing keys {missing}: {entry!r}", file=sys.stderr)
        sys.exit(1)
print(f"PASS — {label}: lineage payload valid (n={len(arr)})")
PY
}

# Like validate_lineage_payload but ALSO asserts non-empty.
validate_non_empty_lineage() {
  local payload="$1"
  local label="$2"
  validate_lineage_payload "$payload" "$label" || return 1
  python3 - "$payload" "$label" <<'PY' || return 1
import json, sys
arr = json.loads(sys.argv[1])
label = sys.argv[2]
if len(arr) == 0:
    print(f"FAIL — {label}: lineage array is empty (expected non-empty)", file=sys.stderr)
    sys.exit(1)
print(f"PASS — {label}: lineage non-empty (n={len(arr)})")
PY
}

# Like validate_lineage_payload but asserts EMPTY array (literal "[]").
validate_empty_lineage() {
  local payload="$1"
  local label="$2"
  validate_lineage_payload "$payload" "$label" || return 1
  python3 - "$payload" "$label" <<'PY' || return 1
import json, sys
arr = json.loads(sys.argv[1])
label = sys.argv[2]
if len(arr) != 0:
    print(f"FAIL — {label}: lineage array expected empty, got n={len(arr)}: {arr!r}", file=sys.stderr)
    sys.exit(1)
print(f"PASS — {label}: lineage explicit-empty []")
PY
}

# Assert lineage contains an entry with given (table, format).
assert_lineage_contains() {
  local payload="$1"
  local table="$2"
  local format="$3"
  local label="$4"
  python3 - "$payload" "$table" "$format" "$label" <<'PY' || return 1
import json, sys
payload, table, fmt, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
arr = json.loads(payload)
match = [e for e in arr if e.get("table") == table and e.get("format") == fmt]
if not match:
    print(f"FAIL — {label}: lineage missing entry (table={table!r}, format={fmt!r}); got: {[(e.get('table'), e.get('format')) for e in arr]}", file=sys.stderr)
    sys.exit(1)
print(f"PASS — {label}: lineage contains (table={table}, format={fmt})")
PY
}

# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

FAILS=0
fail() { echo "$1" >&2; FAILS=$((FAILS + 1)); }

echo "==> health probes (must return X-Data-Lineage: [])"
DEX_HEALTH=$(fetch_lineage_header "${DEX_BASE}/health")
validate_empty_lineage "$DEX_HEALTH" "DEX /health" || fail "DEX /health failed"

# hq-x exposes /healthz, not /health (verified via curl).
HQX_HEALTH=$(fetch_lineage_header "${HQX_BASE}/healthz")
validate_empty_lineage "$HQX_HEALTH" "hq-x /healthz" || fail "hq-x /healthz failed"

echo ""
echo "==> 5-endpoint verification gate (3 DEX + 2 hq-x)"

# DEX endpoint 1: Phase 0a sources — non-empty (3 ops.* Postgres entries)
DEX_SOURCES=$(fetch_lineage_header "${DEX_BASE}/api/v1/internal/observability/sources")
validate_non_empty_lineage "$DEX_SOURCES" "DEX /api/v1/internal/observability/sources" || fail "DEX sources empty"
assert_lineage_contains "$DEX_SOURCES" "ops.data_sources" "postgres_ops" "DEX sources entry: ops.data_sources" || fail "DEX sources missing ops.data_sources entry"
assert_lineage_contains "$DEX_SOURCES" "ops.data_source_slas" "postgres_ops" "DEX sources entry: ops.data_source_slas" || fail "DEX sources missing ops.data_source_slas entry"
assert_lineage_contains "$DEX_SOURCES" "ops.data_source_ingest_runs" "postgres_ops" "DEX sources entry: ops.data_source_ingest_runs" || fail "DEX sources missing ops.data_source_ingest_runs entry"

# DEX endpoint 2: a 401 probe (no auth header) — header MUST be present
# even on auth failures. This proves the middleware runs OUTSIDE the auth
# dependency. Empty `[]` lineage expected.
DEX_401=$(curl -sS -i -o /dev/null -D - "${DEX_BASE}/api/v1/internal/observability/sources" 2>&1 | grep -iE '^x-data-lineage:' | head -n1 | sed -E 's/^[Xx]-[Dd]ata-[Ll]ineage:[[:space:]]*//' | tr -d '\r')
validate_lineage_payload "$DEX_401" "DEX 401 probe" || fail "DEX 401 probe header missing"

# DEX endpoint 3: a 422 probe (POST malformed JSON to fmcsa search) — same
# point: middleware injects header even on validation failures.
DEX_422=$(curl -sS -i -o /dev/null -D - -X POST -H "Authorization: Bearer $DEX_API_KEY" -H "Content-Type: application/json" -d 'malformed' "${DEX_BASE}/api/v1/fmcsa/carriers/search" 2>&1 | grep -iE '^x-data-lineage:' | head -n1 | sed -E 's/^[Xx]-[Dd]ata-[Ll]ineage:[[:space:]]*//' | tr -d '\r')
validate_lineage_payload "$DEX_422" "DEX 422 probe (POST malformed body)" || fail "DEX 422 probe header missing"

# hq-x endpoint 1: observability sources proxy — non-empty (DEX-merged 3 ops.* entries)
# Uses TRIGGER_SHARED_SECRET (hq-x flexible auth — DEX_SERVICE_TOKEN
# isn't accepted by hq-x; that bearer is for DEX direct calls).
HQX_SOURCES=$(fetch_lineage_header "${HQX_BASE}/api/v1/observability/sources" "$HQX_TRIGGER_SECRET")
validate_non_empty_lineage "$HQX_SOURCES" "hq-x /api/v1/observability/sources" || fail "hq-x sources empty"
assert_lineage_contains "$HQX_SOURCES" "ops.data_sources" "postgres_ops" "hq-x sources merge: ops.data_sources" || fail "hq-x sources missing DEX-merged ops.data_sources"
assert_lineage_contains "$HQX_SOURCES" "ops.data_source_slas" "postgres_ops" "hq-x sources merge: ops.data_source_slas" || fail "hq-x sources missing DEX-merged ops.data_source_slas"
assert_lineage_contains "$HQX_SOURCES" "ops.data_source_ingest_runs" "postgres_ops" "hq-x sources merge: ops.data_source_ingest_runs" || fail "hq-x sources missing DEX-merged ops.data_source_ingest_runs"

# hq-x endpoint 2: a non-DEX-proxy hq-x endpoint — header present, may be empty
HQX_HEALTH_PROBE=$(fetch_lineage_header "${HQX_BASE}/healthz" "")
validate_lineage_payload "$HQX_HEALTH_PROBE" "hq-x /healthz (header-present probe)" || fail "hq-x /healthz header missing"

echo ""
if [[ "$FAILS" -gt 0 ]]; then
  echo "FAIL — Phase 0b lineage harness: $FAILS check(s) failed" >&2
  exit 1
fi
echo "PASS — Phase 0b lineage harness: all checks passed"
exit 0
