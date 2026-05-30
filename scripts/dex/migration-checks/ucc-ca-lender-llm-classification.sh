#!/usr/bin/env bash
# Verification harness for cycle `ucc-ca-lender-llm-classification` (2026-05-20).
#
# The corrected CA UCC equipment-finance-lender classifier — an LLM website-
# classification pipeline + a Pattern A Lance dataset. Supersedes the disproven
# debtor-industry-share rollup (PRs #590/#593, reverted by #595).
#
# Structural precedents:
#   - sam-construction-opps-sized.sh / warn-notices-ingest.sh — harness shape
#     (surface-per-check, STRICT gating, _lib-shim.sh, HQ_ALL_ROOT override).
#   - build_bridge_sam_construction_contractors_lance.py — the Pattern A
#     enriched-cohort precedent: m1-only catalog migration with
#     audit_ledger_table=NULL, no ops.bridges / ops.*_emit_runs ledger.
#
# This is a COHORT emit, NOT an identity bridge (L28): NO ops.bridges row, NO
# ops.match_method* rows, NO ledger table.
#
# Surfaces (3 hard code/migration surfaces + 4 Lance/data-quality verify-only):
#   Phase 1 — Migration:  m1 (catalog row + status-view row, audit_ledger NULL)
#   Phase 2 — Code:       c1 (pipeline script)
#   Phase 3 — Lance:      e1 (dataset present, row floor 2000, BTREE)   [SOFT]
#   Phase 4 — Schema:     e2 (classification cols present + enum-subset) [SOFT]
#   Phase 5 — Smoke gate: e3 (HARD premise check: >=60 website-classified
#                             independent_equipment_finance_or_leasing)  [SOFT*]
#   Phase 6 — Ground truth: e4 (6 named proof lenders carry the target subtype) [SOFT*]
#
# *e3/e4 are run_surface_soft so a pre-emit harness run does not FAIL; with
#  STRICT=1 (post-emit) they become HARD — and e3 is THE premise gate.
#
# Usage:
#   ./ucc-ca-lender-llm-classification.sh                  # all (e1-e4 soft)
#   ./ucc-ca-lender-llm-classification.sh --surface m1     # one surface only
#   STRICT=1 ./ucc-ca-lender-llm-classification.sh         # post-emit: e1-e4 HARD
#
# Worktree-aware invocation per L61:
#   HQ_ALL_ROOT=/Users/benjamincrane/hq-all/.claude/worktrees/scope-ucc-lender-llm \
#   STRICT=1 \
#     bash apps/data-engine-x/scripts/migration-checks/ucc-ca-lender-llm-classification.sh

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

echo "==> Verifying ucc-ca-lender-llm-classification (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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

# --- pinned constants per cycle (validator-frozen) ---------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="polaris-warehouse/ucc_ca/lender_classification_lance"
LANCE_URI="s3://${R2_BUCKET}/${R2_PREFIX}"
MIN_ROW_FLOOR=2000           # validator-frozen — spine ~2,111 at total_filings>=100
MIN_EQUIP_FINANCE=60         # validator-frozen — smoke-gate floor
LENDER_KEY="lender_name_normalized"
TARGET_SUBTYPE="independent_equipment_finance_or_leasing"
PIPELINE="$APP_DIR/scripts/build_ucc_ca_lender_classification_lance.py"

# ====================================================================== #
# Phase 1 — Migration
# ====================================================================== #

# ── m1: data_source_catalog ucc_ca_lender_classification row + status view ── #
# Cohort emit: audit_ledger_table is NULL (no ledger). The status view
# (FROM ops.data_source_catalog LEFT JOIN ledger_aggregates) surfaces the new
# row automatically with NULL run-aggregates — assert BOTH the catalog row and
# the status-view row.
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''ucc_ca_lender_classification'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]] &&
  LEDGER_NULL=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''ucc_ca_lender_classification'\'' AND audit_ledger_table IS NULL" | tr -d "[:space:]") &&
  [[ "$LEDGER_NULL" = "1" ]] &&
  VIEW_EXISTS=$(dex_psql_query "SELECT 1 FROM information_schema.views WHERE table_schema='\''ops'\'' AND table_name='\''data_source_catalog_status'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_EXISTS" = "1" ]] &&
  VIEW_BRANCH=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''ucc_ca_lender_classification'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_BRANCH" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/build_ucc_ca_lender_classification_lance.py ──────────── #
# Pattern A enriched-cohort LLM-classification pipeline. Greps positive for the
# required identifiers (the model literal, the async/Semaphore concurrency
# primitives per validator P3, the normalize_entity_name join per P1, the
# fetch_status handling per P4, the classification columns, the Lance-write
# safety primitives per P6); negative for anti-patterns (L42/L54/L59 + a serial
# synchronous Anthropic() client + an ops.bridges write).
run_surface "c1" "bencrane/hq-all" '
  F="'"$PIPELINE"'" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "lender_classification_lance"          "$F" &&
  grep -q "claude-sonnet-4-6"                     "$F" &&
  grep -q "import asyncio"                        "$F" &&
  grep -qE "asyncio\.Semaphore"                   "$F" &&
  grep -qE "asyncio\.(gather|as_completed|create_task)" "$F" &&
  grep -qE "AsyncAnthropic"                       "$F" &&
  grep -q "cache_control"                         "$F" &&
  grep -q "normalize_entity_name"                 "$F" &&
  grep -q "secured_party_name_normalized"         "$F" &&
  grep -q "pdl_website"                           "$F" &&
  grep -q "fetch_status"                          "$F" &&
  grep -q "classification_method"                 "$F" &&
  grep -q "metadata_only"                         "$F" &&
  grep -q "equipment_finance_confidence"          "$F" &&
  grep -q "is_lender"                             "$F" &&
  grep -q "'"$TARGET_SUBTYPE"'"                   "$F" &&
  grep -q "lance_commit_lock"                     "$F" &&
  grep -qE "create_scalar_index"                  "$F" &&
  grep -qE "BTREE"                                "$F" &&
  grep -qE "compact_files"                        "$F" &&
  grep -qE "cleanup_old_versions"                 "$F" &&
  grep -q "init_polaris_lance_generic"            "$F" &&
  grep -qE "mode *= *[\"'\'']overwrite[\"'\'']"   "$F" &&
  grep -qE -e "--apply" -e "\"apply\""            "$F" &&
  grep -q "run_id"                                "$F" &&
  grep -q "generated_at"                          "$F" &&
  ! grep -qE "^[^#]*duckdb\\.typing\\."           "$F" &&
  ! grep -qE "^[^#]*Content-Encoding.*zstd"       "$F" &&
  ! grep -qE "^[^#]*LIST<VARCHAR>"                "$F" &&
  ! grep -qiE "INSERT +INTO +ops\\.bridges"       "$F" &&
  ! grep -qiE "INSERT +INTO +ops\\.match_method"  "$F" &&
  ! grep -qE "[^c_]Anthropic\\(\\)"               "$F"
'

# ====================================================================== #
# Phase 3 — Lance dataset (SOFT until post-emit; HARD with STRICT=1)
# ====================================================================== #

# ── e1: Lance dataset present — row floor 2000 + BTREE on lender key ──── #
_E1_PY=$(mktemp -t lance_e1.XXXXXX.py)
cat >"$_E1_PY" <<'PYEOF'
import lance, os
ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lender_classification_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
rows = ds.count_rows()
assert rows >= 2000, f"e1 floor breach: {rows} < 2000"
idx_cols = []
for i in ds.list_indices():
    idx_cols.extend(i.get("fields", []))
assert "lender_name_normalized" in idx_cols, \
    f"e1 BTREE on lender_name_normalized missing: {idx_cols}"
print(f"e1 ok: rows={rows} idx={sorted(idx_cols)}")
PYEOF
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 4 — Schema + enum-subset (SOFT until post-emit; HARD with STRICT=1)
# ====================================================================== #

# ── e2: classification columns present + every distinct value ∈ declared enum ── #
# The pipeline declares SUBTYPE_ENUM / IS_LENDER_ENUM / FETCH_STATUS_ENUM /
# CLASSIFICATION_METHOD_ENUM as module-level constants. The harness imports the
# pipeline module and reads those constants directly (no drift between the
# declared enum and the checked enum), then asserts every emitted distinct
# value is a member. This is the validator P5 internal-consistency check.
_E2_PY=$(mktemp -t lance_e2.XXXXXX.py)
cat >"$_E2_PY" <<'PYEOF'
import importlib.util, os, sys
import lance, duckdb

PIPE = os.path.join(
    os.environ["APP_DIR"], "scripts",
    "build_ucc_ca_lender_classification_lance.py",
)
spec = importlib.util.spec_from_file_location("ucc_pipe", PIPE)
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.join(os.environ["APP_DIR"]))
spec.loader.exec_module(mod)

declared = {
    "subtype": set(mod.SUBTYPE_ENUM),
    "is_lender": set(mod.IS_LENDER_ENUM),
    "fetch_status": set(mod.FETCH_STATUS_ENUM),
    "classification_method": set(mod.CLASSIFICATION_METHOD_ENUM),
}

ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lender_classification_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
schema_cols = {f.name for f in ds.schema}
required = {
    "is_lender", "subtype", "equipment_finance_confidence",
    "fetch_status", "classification_method", "rationale",
}
missing = required - schema_cols
assert not missing, f"e2 FAIL: classification columns missing: {missing}"
print(f"e2 ok: all {len(required)} classification columns present")

tbl = ds.scanner(
    columns=["subtype", "is_lender", "fetch_status", "classification_method"],
).to_table()
con = duckdb.connect()
con.register("d", tbl)
for col, allowed in declared.items():
    vals = {
        r[0] for r in con.execute(
            f"SELECT DISTINCT {col} FROM d WHERE {col} IS NOT NULL"
        ).fetchall()
    }
    rogue = vals - allowed
    assert not rogue, (
        f"e2 FAIL: {col} has values outside the declared enum: {rogue} "
        f"(declared: {sorted(allowed)})"
    )
    print(f"e2 ok: {col} distinct={sorted(vals)} ⊆ declared enum")
PYEOF
run_surface_soft "e2" "bencrane/hq-all" "
  cd '$APP_DIR' && APP_DIR='$APP_DIR' doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# ====================================================================== #
# Phase 5 — Smoke gate (the HARD premise check; SOFT pre-emit only)
# ====================================================================== #

# ── e3: >= 60 rows subtype=independent_equipment_finance_or_leasing AND ── #
#        classification_method != 'metadata_only' (website-based).
# This is THE premise check — it confirms the pipeline actually produced the
# operator's target population via website classification, not merely "a
# dataset exists". The lesson from PRs #590/#593, whose harnesses checked
# structure but never the premise.
_E3_PY=$(mktemp -t lance_e3.XXXXXX.py)
cat >"$_E3_PY" <<'PYEOF'
import lance, os, duckdb
ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lender_classification_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
tbl = ds.scanner(
    columns=["subtype", "classification_method"],
).to_table()
con = duckdb.connect()
con.register("d", tbl)
n = con.execute(
    "SELECT count(*) FROM d "
    "WHERE subtype = 'independent_equipment_finance_or_leasing' "
    "  AND classification_method <> 'metadata_only'"
).fetchone()[0]
assert n >= 60, (
    f"e3 SMOKE GATE FAIL: only {n} website-classified "
    f"independent_equipment_finance_or_leasing rows (floor 60) -- the "
    f"pipeline did not produce the operator's target population"
)
print(f"e3 ok: {n} website-classified independent_equipment_finance_or_leasing rows (>= 60)")

# Observability — full subtype distribution.
print("e3 subtype distribution:")
for subtype, c in con.execute(
    "SELECT subtype, count(*) AS n FROM d GROUP BY subtype ORDER BY n DESC"
).fetchall():
    print(f"    {subtype}: {c}")
PYEOF
run_surface_soft "e3" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E3_PY'
"

# ====================================================================== #
# Phase 6 — Ground-truth spot-check (SOFT pre-emit only)
# ====================================================================== #

# ── e4: the 6 named proof lenders carry the target subtype ───────────── #
# Of the 6 lenders the 2026-05-20 volume-spine proof named as independent
# equipment finance, those present in the classified output must carry
# subtype='independent_equipment_finance_or_leasing'. Mitsubishi HC Capital is
# allowed borderline (may carry oem_captive_finance / bank_or_depository
# without failing). Match by normalize_entity_name-equality, NOT raw strings.
_E4_PY=$(mktemp -t lance_e4.XXXXXX.py)
cat >"$_E4_PY" <<'PYEOF'
import lance, os, sys
sys.path.insert(0, os.path.join(os.environ["APP_DIR"]))
from scripts._lib.entity_name_normalize import normalize_entity_name

ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lender_classification_lance",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
rows = ds.scanner(
    columns=["lender_name_normalized", "subtype", "equipment_finance_confidence"],
).to_table().to_pylist()

# norm-key -> (subtype, confidence). A norm-key can collapse multiple raw spine
# names; if so, prefer any row carrying the target subtype.
by_norm = {}
for r in rows:
    k = normalize_entity_name(r["lender_name_normalized"])
    if k is None:
        continue
    cur = by_norm.get(k)
    if cur is None or (
        r["subtype"] == "independent_equipment_finance_or_leasing"
        and cur[0] != "independent_equipment_finance_or_leasing"
    ):
        by_norm[k] = (r["subtype"], r["equipment_finance_confidence"])

# The 6 proof lenders. Mitsubishi HC Capital is borderline-allowed.
PROOF = [
    ("Geneva Capital", False),
    ("Marlin Business Bank", False),
    ("Western Equipment Finance", False),
    ("Commercial Credit Group", False),
    ("Cornerstone Financial Services", False),
    ("Mitsubishi HC Capital", True),   # borderline allowed
]
TARGET = "independent_equipment_finance_or_leasing"

failures = []
found = 0
for name, borderline in PROOF:
    k = normalize_entity_name(name)
    hit = by_norm.get(k)
    if hit is None:
        print(f"e4 note: {name!r} (norm={k!r}) not in classified output -- skipped")
        continue
    found += 1
    subtype, conf = hit
    ok = subtype == TARGET
    if borderline:
        # borderline: oem_captive_finance / bank_or_depository also acceptable
        ok = ok or subtype in ("oem_captive_finance", "bank_or_depository")
    status = "OK" if ok else "FAIL"
    print(f"e4 {status}: {name!r} -> subtype={subtype} conf={conf}")
    if not ok:
        failures.append((name, subtype))

assert found >= 1, "e4 FAIL: none of the 6 proof lenders are in the output"
assert not failures, f"e4 FAIL: proof lenders misclassified: {failures}"
print(f"e4 ok: {found}/6 proof lenders present, all carry the expected subtype")
PYEOF
run_surface_soft "e4" "bencrane/hq-all" "
  cd '$APP_DIR' && APP_DIR='$APP_DIR' doppler run --project hq-all --config prd -- uv run python '$_E4_PY'
"

# --- cleanup temp .py checkers ------------------------------------------ #
_cleanup_tmp() { rm -f "$_E1_PY" "$_E2_PY" "$_E3_PY" "$_E4_PY"; }
trap _cleanup_tmp EXIT

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
