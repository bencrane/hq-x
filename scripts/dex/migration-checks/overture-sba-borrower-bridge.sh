#!/usr/bin/env bash
# Verification harness for /scope cycle overture-sba-borrower-bridge.
#
# SKELETON authored by validator; per-surface verify bodies are filled by
# the audit stage from the directive's authoritative s1-s8 list.
#
# Mirrors ucc-gleif-identity-spine.sh (most recent precedent in this scope
# family — 2026-05-13). Re-uses _lance_floor_check + _polaris_lance_check.
#
# Audit MUST:
#   1) READ the validator's `## Validator notes` block in the directive
#      first — it carries the verbatim Overture schema, sampled access
#      paths, and known traps (geometry coords appear scrambled in the
#      current 2026-04-15.0 release; zip is mixed 5-digit / zip+4; only
#      1,047 distinct brand.wikidata IDs exist in the US slice, ~1.86%
#      of rows).
#   2) Write per-surface bodies in the placeholder blocks below.
#   3) Use Arrow-bridge pattern for Lance reads (NOT lance-duckdb
#      extension; unstable on osx_arm64 per Lance canary cycle).
#   4) Use ON CONFLICT (display_name) for ops.data_sources UPSERT (s3).
#   5) Pre-normalize SBA borrower names in Python at script-time (per
#      prior cycle's OOM fix); register the canonical
#      `scripts/_lib/entity_name_normalize.py` as a DuckDB UDF on the
#      Overture side OR pre-normalize Overture too. Audit picks one.
#   6) Substr the postcode to first 5 chars before joining (zip+4 is the
#      majority shape on Overture's US slice).
#   7) Skip the geometry column at read level OR document the
#      lat/lon-scrambling workaround discovered during validation.
#      The directive's "extract lat/lon via .x/.y accessors" approach
#      is INSUFFICIENT — coords come out wrong for the 2026-04-15.0
#      release. Audit must specify the working extraction shape (PyArrow
#      compute? bbox.xmin/ymin? operator-deferred?) OR drop lat/lon from
#      the v1 Lance schema entirely (it is not load-bearing for the
#      smoke; reviewer can add a follow-up cycle if needed).

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

# --- hq-x DB helpers (HQX_DB_URL_POOLED, distinct from DEX) -------------- #
# Doppler shell gotcha: env vars must be referenced inside bash -c '...' so
# Doppler expands them at runtime, not at compose time. See
# apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha".
_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}

hqx_psql_query() {
  local sql="$1"
  _hqx_doppler "psql \"\$HQX_DB_URL_POOLED\" -tAc \"$sql\""
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
# Exits 0 iff Lance dataset count_rows() >= floor. Uses Arrow-bridge
# pattern (lance.dataset + count_rows); avoids lance-duckdb extension.
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 -c "
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
"
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet python apps/data-engine-x/scripts/init_polaris_lance_generic.py \
      --namespace "$ns" --table "$tbl" --check-only
}

# ── s1: Overture US Places Lance-emit ──────────────────────────────────── #
# Floor: 12,000,000 per directive (80% of 15.95M raw US-filtered rows).
# Validator-confirmed raw count: 15,952,626. The emit must filter
# `addresses[1].country = 'US'` (DuckDB 1-based list indexing) and flatten
# nested types (names struct → name_primary; addresses[1] struct →
# address_freeform/locality/postcode/region; brand struct → brand_wikidata
# + brand_name_primary; phones[]/websites[]/emails[] → first element).
# DROP the `geometry` and `sources` columns (validator finding: geometry
# coords are scrambled in 2026-04-15.0 release; sources is deeply nested).
run_surface "s1" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_overture_us_places_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance/" 12000000
'

# ── s2: Overture × SBA Borrower bridge ─────────────────────────────────── #
# Floor: 100,000 per directive (1% of 10.85M distinct SBA composite keys).
# Validator-confirmed:
#   - SBA borrowers_lance: 12,008,176 rows; 10,846,461 distinct
#     (legal_name_normalized, borrstate, substr(borrzip,1,5)) tuples;
#     12,008,118 rows have all three non-null (99.9995% coverage).
#   - Overture US: 15,952,626 rows; 14,722,462 distinct
#     (normalize(names.primary), region, substr(postcode,1,5)) tuples;
#     15,449,973 with full key (96.8%).
# Audit must:
#   - Substring postcode to 5 chars on BOTH sides (Overture has 56% zip+4,
#     SBA borrzip is mostly 9-digit).
#   - Pre-normalize Overture's names.primary using the canonical
#     scripts/_lib/entity_name_normalize.py (register as DuckDB UDF).
#   - Pre-dedupe each side to distinct composite keys BEFORE the join
#     (per UCC × PDL cycle finding: dedup-first prevents hash-join OOM).
#   - Emit `match_path` column with values 'direct' (composite-key match)
#     so the smoke can later distinguish from any future brand-only path.
#   - Audit row in ops.bridge_generation_runs.bridge_name='sba_overture_places'.
run_surface "s2" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_sba_overture_places_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_overture_places_lance/" 100000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sba_overture_places'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s3: ops.data_sources migration applied ─────────────────────────────── #
# 2 new display_name rows expected. Audit uses ON CONFLICT (display_name)
# per prior cycle pattern (display_name is the UNIQUE key; source_id is
# gen_random_uuid PK).
run_surface "s3" "hq-all" '
  COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE format='"'"'lance'"'"' AND owner_app='"'"'data-engine-x'"'"' AND status='"'"'active'"'"' AND display_name IN ('"'"'overture_us_places_lance'"'"','"'"'bridges_sba_overture_places_lance'"'"')") &&
  test "$COUNT" = "2"
'

# ── s4: matching-engine ENTITY_REF_COLUMNS extension ───────────────────── #
# Per directive s4: append 2 entries to ENTITY_REF_COLUMNS in engine.py.
# Append-only — DO NOT remove existing entries.
run_surface "s4" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"overture\", \"us_places_lance\"" "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"sba_overture_places_lance\"" "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py"
'

# ── s5: Polaris Generic Table API registrations (both) ─────────────────── #
run_surface "s5" "hq-all" '
  _polaris_lance_check "overture" "us_places_lance"          &&
  _polaris_lance_check "bridges"  "sba_overture_places_lance"
'

# ── s6: backfill (composite — re-run s1+s2 row-floor checks) ───────────── #
# Backfill is a runbook step, not a separate artifact. Its verify is the
# union of s1+s2 row-count floors passing AFTER --apply has been run.
run_surface "s6" "hq-all" '
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance/"          12000000 &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_overture_places_lance/" 100000
'

# ── s7: hq-x Railway deploy + runtime probe ────────────────────────────── #
# Skip in pre-deploy mode (no MERGE_SHA env); deploy-verifier sets MERGE_SHA.
# Per directive s7: DEX no API surface change; only hq-x redeploys.
# Per apps/data-engine-x/CLAUDE.md §"Deploy verification" use the runtime-probe
# helper — health-check alone is insufficient (2026-05-12 numpy incident).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s7-deploy-hqx" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"hq-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s7-runtime-probe-hqx" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime hq-x "https://api.opsengine.run"
  '
else
  echo "-- s7 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify for hq-x)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s8: end-to-end smoke (gated by SMOKE_E2E=1) ────────────────────────── #
# Per directive `## Goal restated` #4: for the TX-franchisee-COMMIT spec's
# matched borrowers in business.matches, verify ≥50 (≥12% of the 424
# documented matched borrowers) have a non-null phone_primary OR
# website_primary in bridges.sba_overture_places_lance. Audit specifies
# the exact JOIN shape against ops.audience_specs / business.matches /
# business.audience_spec_signings depending on where the 424 cohort lives.
# Smoke ONLY meaningful AFTER s7 deploys + ENTITY_REF_COLUMNS is wired
# (s4) + bridge exists with backfill applied (s6).
if [[ -n "${SMOKE_E2E:-}" ]]; then
  run_surface "s8-end-to-end-smoke" "hq-all" '
    ENRICHED=$(doppler run --project hq-all --config prd -- \
      uv run --quiet --with pylance --with duckdb python3 -c "
import os, lance, duckdb
so = {\"aws_endpoint\": os.environ[\"R2_ENDPOINT\"], \"aws_access_key_id\": os.environ[\"R2_ACCESS_KEY_ID\"], \"aws_secret_access_key\": os.environ[\"R2_SECRET_ACCESS_KEY\"], \"aws_region\": \"us-east-1\", \"aws_virtual_hosted_style_request\": \"false\"}
sba_ds = lance.dataset(\"s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/\", storage_options=so)
br_ds = lance.dataset(\"s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_overture_places_lance/\", storage_options=so)
con = duckdb.connect()
con.register(\"sba\", sba_ds.to_table())
con.register(\"br\", br_ds.to_table())
n = con.execute(\"\"\"
  SELECT COUNT(DISTINCT sba.legal_name_normalized || sba.borrstate)
  FROM sba JOIN br
    ON sba.legal_name_normalized = br.sba_legal_name_normalized
   AND sba.borrstate = br.sba_borrstate
   AND substr(replace(cast(sba.borrzip AS VARCHAR), '\''.0'\'', '\'''\''), 1, 5) = br.sba_borrzip5
  WHERE sba.has_pending_commit = TRUE
    AND (br.phone_primary IS NOT NULL OR br.website_primary IS NOT NULL)
\"\"\").fetchone()[0]
print(n)
") &&
    test "$ENRICHED" -ge "1000"
  '
else
  echo "-- s8-end-to-end-smoke (hq-all): SKIPPED (set SMOKE_E2E=1 to run)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
