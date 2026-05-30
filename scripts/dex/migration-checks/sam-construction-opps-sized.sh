#!/usr/bin/env bash
# Verification harness for cycle `sam-construction-opps-sized` (2026-05-19).
#
# Pattern A enriched-cohort emit — open federal construction opportunities,
# each banded by an expected-award-size derived from the historical
# USAspending base-award-size distribution.
#
#   LEFT (spine):   sam_gov/opps_active_lance — open construction opps
#                   (naics_code LIKE '23%' AND response_deadline > now()).
#   RIGHT (distrib): usaspending/contracts_lance — construction base awards
#                   (naics_code LIKE '23%' AND modification_number = '0').
#   Output:         bridges/construction_opps_sized_lance — one row per open
#                   construction opp, PK notice_id.
#
# Structural precedents:
#   - PR #571 usaspending-sos-ny-owner-bridge — harness shape (post-PR-#570:
#     sys.path /root/scripts, comment-safe negation greps, STRICT gating).
#   - bridges.sam_construction_contractors_lance
#     (build_bridge_sam_construction_contractors_lance.py) — the namespace +
#     enriched-cohort precedent: bridges/ namespace, m1-only catalog migration
#     with audit_ledger_table=NULL, no ops.bridges / ops.*_emit_runs ledger.
#
# This is a COHORT emit, NOT an identity bridge (per L28): there is NO
# ops.bridges row, NO ops.match_method* rows, NO ledger table. e2 here is a
# data-QUALITY gate on the emitted Lance dataset (the "not foolishly off"
# check), NOT a REUSER method-row-parity check.
#
# Surfaces (4 hard code/migration surfaces + 3 post-deploy verify-only):
#   Phase 1 — Migrations:    m1 (catalog row + view recreation)
#   Phase 2 — Code:          c1 (emit script), c2 (Modal cron), c6 (LanceViews entry)
#   Phase 3 — R2:            r1 (Lance dataset present)               [SOFT — strict post-deploy]
#   Phase 4 — Lance:         e1 (row floor + 4× BTREE)                [SOFT — strict post-deploy]
#   Phase 5 — Data quality:  e2 (no-null bands + 237>238 directional + tier dist) [SOFT]
#
# Usage:
#   ./sam-construction-opps-sized.sh                  # all surfaces (r1/e1/e2 soft)
#   ./sam-construction-opps-sized.sh --surface m1     # one surface only
#   STRICT=1 ./sam-construction-opps-sized.sh         # post-deploy: missing r1/e1/e2 FAIL
#
# Worktree-aware invocation per L61:
#   HQ_ALL_ROOT=/Users/benjamincrane/hq-all/.claude/worktrees/competent-rubin-fcce42 \
#   STRICT=1 \
#     bash apps/data-engine-x/scripts/migration-checks/sam-construction-opps-sized.sh

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
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

APP_DIR="$HQ_ALL_ROOT/apps/data-engine-x"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

# --- CLI parsing --------------------------------------------------------- #
REPO_FILTER=""
SURFACE_FILTER=""
STRICT="${STRICT:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --strict)  STRICT=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying sam-construction-opps-sized (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
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

run_surface_soft() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ "$STRICT" -eq 1 ]]; then
    run_surface "$id" "$repo" "$cmd"
    return 0
  fi
  echo "-- $id ($repo): RUNNING (soft)"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): SKIP (artifact not yet present; STRICT=1 to FAIL)"
    SKIP_COUNT=$((SKIP_COUNT+1))
  fi
}

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="polaris-warehouse/bridges/construction_opps_sized_lance"
LANCE_URI="s3://${R2_BUCKET}/${R2_PREFIX}"
FLOOR_ROWS=1400          # dataset floor (validator-calibrated 2026-05-19; ~62% of 2,261 live)
MIN_R2_OBJECT_FLOOR=1    # one Lance dataset under the prefix

# ====================================================================== #
# Phase 1 — Migrations
# ====================================================================== #

# ── m1: data_source_catalog INSERT + view recreation ─────────────────── #
# Cohort emit: audit_ledger_table column is NULL (no ledger table) — mirrors
# bridges.sam_construction_contractors. The status-view branch for
# sam_construction_opps_sized is the catalog-only branch shape (it reads
# ops.data_source_catalog directly, not a ledger — see the m1 spec in
# the directive ## Audit plan).
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''sam_construction_opps_sized'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]] &&
  LEDGER_NULL=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''sam_construction_opps_sized'\'' AND audit_ledger_table IS NULL" | tr -d "[:space:]") &&
  [[ "$LEDGER_NULL" = "1" ]] &&
  VIEW_EXISTS=$(dex_psql_query "SELECT 1 FROM information_schema.views WHERE table_schema='\''ops'\'' AND table_name='\''data_source_catalog_status'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_EXISTS" = "1" ]] &&
  VIEW_BRANCH=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''sam_construction_opps_sized'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_BRANCH" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_sam_construction_opps_sized_emit.py ──────────────── #
# Pattern A enriched-cohort emit. Greps positive for required identifiers
# (tier logic T2..T5, the award-size field, the join keys, BTREE cols);
# negative for anti-patterns (L42/L54/L59) AND for any scoring column —
# scoring is the FOLLOW-ON cycle sam-construction-opps-score, explicitly
# out of scope here.
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sam_construction_opps_sized_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "construction_opps_sized_lance"          "$F" &&
  grep -q "base_and_all_options_value"             "$F" &&
  grep -q "modification_number"                    "$F" &&
  grep -q "lance_commit_lock"                      "$F" &&
  grep -q "naics_code"                             "$F" &&
  grep -q "primary_place_of_performance_state_code" "$F" &&
  grep -q "pop_state"                              "$F" &&
  grep -q "response_deadline"                      "$F" &&
  grep -q "size_basis_tier"                        "$F" &&
  grep -q "size_band"                              "$F" &&
  grep -q "expected_award_value"                   "$F" &&
  grep -q "hist_sample_count"                      "$F" &&
  grep -q "hist_median"                            "$F" &&
  grep -q "hist_p25"                               "$F" &&
  grep -q "hist_p75"                               "$F" &&
  grep -qE "['\''\"]T2['\''\"]"                    "$F" &&
  grep -qE "['\''\"]T3['\''\"]"                    "$F" &&
  grep -qE "['\''\"]T4['\''\"]"                    "$F" &&
  grep -qE "['\''\"]T5['\''\"]"                    "$F" &&
  grep -qE "substr\(.*naics_code.*1, *3\)|SUBSTR\(.*naics_code.*1, *3\)" "$F" &&
  grep -qE "substr\(.*naics_code.*1, *2\)|SUBSTR\(.*naics_code.*1, *2\)" "$F" &&
  grep -q "create_scalar_index"                    "$F" &&
  grep -qE "BTREE"                                 "$F" &&
  grep -q "init_polaris_lance_generic"             "$F" &&
  grep -qE "mode *= *[\"'\'']overwrite[\"'\'']"    "$F" &&
  grep -qE -e "--apply" -e "\"apply\"" -e "'\''apply'\''" "$F" &&
  grep -qE -e "--dry-run" -e "\"dry_run\"" -e "dry-run"  "$F" &&
  ! grep -qE "^[^#]*duckdb\\.typing\\."            "$F" &&
  ! grep -qE "^[^#]*Content-Encoding.*zstd"        "$F" &&
  ! grep -qE "^[^#]*LIST<VARCHAR>"                 "$F" &&
  ! grep -qiE "^[^#]*( AS +[a-z_]*score|[\"'\''][a-z_]*score[\"'\''] +AS )" "$F"
'

# ── c2: modal/sam_construction_opps_sized_app.py ─────────────────────── #
# Modal app name + Cron schedule + secrets + delegates to c1 emit script.
# Per PR #570 lesson: sys.path.insert(0, "/root/scripts") — NOT "/root".
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/sam_construction_opps_sized_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-sam-construction-opps-sized" "$F" &&
  grep -q "Cron("                                     "$F" &&
  grep -q "dex-db"                           "$F" &&
  grep -q "bulk-ingest-r2"                            "$F" &&
  grep -q "timeout="                                  "$F" &&
  grep -q "memory="                                   "$F" &&
  grep -q "run_sam_construction_opps_sized_emit"      "$F" &&
  grep -qE "sys\\.path\\.insert\\(0, *[\"'\'']/root/scripts[\"'\'']\\)" "$F"
'

# ── c6: app/services/lance_views.py — LanceView entry appended ──────── #
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "construction_opps_sized_lance" "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first refresh)
# ====================================================================== #

# ── r1: Lance dataset prefix has ≥1 object ───────────────────────────── #
run_surface_soft "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | wc -l | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
    echo \"r1 ok: objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emit (SOFT until first refresh run)
# ====================================================================== #

# ── e1: Lance row floor ≥ 1,400 + 4× BTREE ───────────────────────────── #
# BTREE columns: notice_id (PK) + pop_state + naics_code + size_band.
_E1_PY=$(mktemp -t lance_e1.XXXXXX.py)
cat >"$_E1_PY" <<'PYEOF'
import lance, os
ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/construction_opps_sized_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
rows = ds.count_rows()
assert rows >= 1400, f"e1 floor breach: {rows} < 1400"
idx_cols = []
for i in ds.list_indices():
    idx_cols.extend(i.get("fields", []))
for col in ("notice_id", "pop_state", "naics_code", "size_band"):
    assert col in idx_cols, f"e1 BTREE on {col} missing: {idx_cols}"
print(f"e1 ok: rows={rows} idx={sorted(idx_cols)}")
PYEOF
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 5 — Data quality (SOFT until first refresh — the load-bearing
# "not foolishly off" gate; can only pass once the emit has written rows).
#
# Real runnable DuckDB-on-Lance assertion on the EMITTED dataset:
#   (a) ZERO rows with NULL size_band / size_basis_tier / expected_award_value
#       — the T5 sector-wide backstop guarantees every opp is banded.
#   (b) directional sanity: median(expected_award_value) for naics 237%
#       (heavy & civil) > median for naics 238% (specialty trades).
#       Validator pre-checked on emitted grain: 237=$521K vs 238=$48K (10.7x).
#   (c) prints the size_basis_tier distribution (observability).
# ====================================================================== #

_E2_PY=$(mktemp -t lance_e2.XXXXXX.py)
cat >"$_E2_PY" <<'PYEOF'
import lance, os, duckdb
ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/construction_opps_sized_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
tbl = ds.scanner(
    columns=["naics_code", "size_band", "size_basis_tier", "expected_award_value"],
).to_table()
con = duckdb.connect()
con.register("d", tbl)

# (a) no-null bands — T5 backstop guarantees every opp is banded.
nulls = con.execute(
    "SELECT count(*) FROM d "
    "WHERE size_band IS NULL OR size_basis_tier IS NULL OR expected_award_value IS NULL"
).fetchone()[0]
assert nulls == 0, f"e2(a) FAIL: {nulls} rows with NULL band/tier/expected_award_value"
print("e2(a) ok: 0 null-band rows")

# (b) directional sanity — 237 (heavy & civil) median > 238 (specialty trades) median.
row = con.execute(
    "SELECT "
    "  median(expected_award_value) FILTER (WHERE naics_code LIKE '237%') AS m237, "
    "  median(expected_award_value) FILTER (WHERE naics_code LIKE '238%') AS m238 "
    "FROM d"
).fetchone()
m237, m238 = row[0], row[1]
assert m237 is not None, "e2(b) FAIL: no naics 237% rows in emitted dataset"
assert m238 is not None, "e2(b) FAIL: no naics 238% rows in emitted dataset"
assert m237 > m238, f"e2(b) FAIL: 237 median ({m237:.0f}) <= 238 median ({m238:.0f}) — derivation foolishly off"
print(f"e2(b) ok: 237 median={m237:.0f} > 238 median={m238:.0f}")

# (c) tier distribution — observability.
dist = con.execute(
    "SELECT size_basis_tier, count(*) AS n "
    "FROM d GROUP BY size_basis_tier ORDER BY size_basis_tier"
).fetchall()
total = sum(n for _, n in dist)
print("e2(c) size_basis_tier distribution:")
for tier, n in dist:
    print(f"    {tier}: {n} ({100.0 * n / total:.1f}%)")
PYEOF
run_surface_soft "e2" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# --- cleanup temp .py checkers ------------------------------------------ #
_cleanup_tmp() { rm -f "$_E1_PY" "$_E2_PY"; }
trap _cleanup_tmp EXIT

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
