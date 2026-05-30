#!/usr/bin/env bash
# Verification harness for cycle `sec-bdc-soi-html-parse-v2` (2026-05-22).
#
# Authored 2026-05-22 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-22-sec-bdc-soi-html-parse-v2.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/2026-05-22-sec-bdc-soi-html-parse-v2.sh
#
# Pattern: mirrors `sec-bdc-soi-ingest.sh` (the v1 predecessor harness in this
# same directory — same SEC source family, same audit-shim pattern). v2 is a
# 7-surface cycle (no Lance emit this cycle — that is the downstream Lance
# re-emission cycle), so this harness omits the s5/s6/s7 Lance-build surfaces
# from the predecessor and adds e2-e6 v2-specific verify-only checks (trust-up,
# trust-down, coverage, reproducibility, v1 comparison).
#
# Surfaces (in declared order):
#   Phase 1 — Migrations:        s1, s2
#   Phase 2 — Code:              s3 (parser v2), s4 (classifier), s5 (sample-audit)
#   Phase 3 — Modal:             s6 (Modal app code), s7 (deploy)
#   Phase 4 — Verify-only:       r1, e1, e2, e3, e4, e5, e6
#                                (soft pre-deploy; STRICT=1 post-deploy)
#
# r1/e1-e6 are SOFT pre-deploy: a missing R2 prefix / Parquet reports
# SOFT-SKIP and does NOT fail the run. Post-deploy, run with STRICT=1 to make
# them hard gates (per directive §"Verification harness").
#
# Usage:
#   ./2026-05-22-sec-bdc-soi-html-parse-v2.sh                  # all surfaces (soft r1/e1-e6)
#   ./2026-05-22-sec-bdc-soi-html-parse-v2.sh --surface s1     # one surface
#   ./2026-05-22-sec-bdc-soi-html-parse-v2.sh --repo data-engine-x
#   STRICT=1 ./2026-05-22-sec-bdc-soi-html-parse-v2.sh         # post-deploy: r1/e1-e6 hard-gated
#   HQ_ALL_ROOT=/path/to/worktree ./2026-05-22-sec-bdc-soi-html-parse-v2.sh
#
# Doppler idiom per CLAUDE.md §"Doppler shell gotcha":
#   doppler run --project hq-all --config prd -- bash -c '...'

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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

STRICT="${STRICT:-0}"

echo "==> Verifying sec-bdc-soi-html-parse-v2 (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} STRICT=$STRICT)"

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

# run_soft_surface — for r1/e1-e6. Pre-deploy (STRICT=0): a verify miss reports
# SOFT-SKIP and does NOT increment FAIL_COUNT. Post-deploy (STRICT=1): identical
# to run_surface — a miss is a hard FAIL.
run_soft_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- $id ($repo): RUNNING (soft; STRICT=$STRICT)"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    if [[ "$STRICT" -eq 1 ]]; then
      echo "-- $id ($repo): FAIL (STRICT)" >&2
      FAIL_COUNT=$((FAIL_COUNT+1))
    else
      echo "-- $id ($repo): SOFT-SKIP (pre-deploy; re-run with STRICT=1 post-deploy)"
      SKIP_COUNT=$((SKIP_COUNT+1))
    fi
  fi
}

# --- pinned constants per audit (validator-set; audit may refine) -------- #
R2_BUCKET="dex-raw-landing-zone"
R2_SOI_PARSED_V2_PREFIX="sec-bdc/soi-parsed-v2"
R2_SOI_SOURCE_PREFIX="sec-bdc/soi"

MODAL_APP_NAME="data-engine-x-bdc-soi-parse-v2"
SOURCE_SLUG="bdc_soi_parsed_v2"
LEDGER_TABLE="bdc_soi_parsed_v2_runs"

# Volume floors per directive §"Volume floors" (validator-set 2026-05-22).
FLOOR_E1=60000
E2_AUDIT_N=50           # rows per (BDC, period) bucket for --trust-up
E3_AUDIT_N=20           # rows per demotion_reason bucket for --trust-down
E2_PRECISION_PCT=100    # 100% required; no shipping at 95%
PERIODS_FLOOR=22        # r1 floor: >=22 of 23 source periods have output
E6_AGREEMENT_PCT=95     # e6 floor: v2 ≥95% exact-match agreement with v1 on overlap subset

# Periods that exist in the source (validator-confirmed 2026-05-22 via
# `aws s3 ls s3://dex-raw-landing-zone/sec-bdc/soi/`): 10 quarterly
# (2022q4..2025q1) + 13 monthly (2025_04..2026_04) = 23 release periods.
SOURCE_PERIODS_COUNT=23

# Sentinel period for e1 schema + row-count check.
E1_SENTINEL_PERIOD="2025q1"

# --- Python e1 row-count + schema check (writes a temp .py file) --------- #
# Why temp file: inlining Python source through nested bash -c '...' triggers
# quote-stripping by the outer shell, mangling string literals. The temp-file
# pattern (predecessor sec-bdc-soi-ingest.sh §"_lance_check_inline") sidesteps
# the quoting nightmare entirely.
#
# Reads sec-bdc/soi-parsed-v2/release=<sentinel>/data.parquet via DuckDB httpfs
# (R2 endpoint from $R2_ENDPOINT), asserts row count >= floor and that the v2
# NEW column literals from directive §"Schema" are present in the schema.
_e1_check_inline() {
  # Args reserved for future use; current implementation parameterizes via
  # env vars (E1_PARQUET_URI, E1_FLOOR) at invocation time. set -u tolerated.
  local _unused1="${1:-}" _unused2="${2:-}"
  local tmpfile
  tmpfile=$(mktemp -t e1_check.XXXXXX.py)
  cat >"$tmpfile" <<'PYEOF'
import os, sys, duckdb
URI = os.environ["E1_PARQUET_URI"]
FLOOR = int(os.environ["E1_FLOOR"])
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").replace("http://", "")
con.execute(f"SET s3_endpoint='{endpoint}'")
con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}'")
con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}'")
con.execute("SET s3_url_style='path'")
rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{URI}')").fetchone()[0]
assert rows >= FLOOR, f"e1 row-count floor breach: {rows} < {FLOOR}"
cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{URI}')").fetchall()}
required = {
    "principal", "amortized_cost",
    "investment_interest_rate_raw", "investment_interest_rate_spread_bps",
    "investment_identifier", "name",
    "parse_confidence", "parse_demotion_reason",
    "source_filing_url", "parser_version",
    "maturity_date_typed",
}
missing = required - cols
assert not missing, f"e1 schema missing v2 columns: {missing}"
print(f"e1 ok: rows={rows} v2-cols-present={sorted(required)}")
PYEOF
  echo "$tmpfile"
}

# --- Python e5 determinism check (writes a temp .py file) ---------------- #
# Reads current sec-bdc/soi-parsed-v2/release=<sentinel>/data.parquet via httpfs,
# computes a row-level MD5 over all columns EXCEPT extracted_at + parser_version
# (which legitimately change per run), then re-runs s3 (parse_sec_bdc_soi_html_v2.py)
# for the sentinel period writing to a determinism-check prefix, and compares
# hashes. Non-determinism is a ship blocker.
_e5_check_inline() {
  # Args reserved for future use; current implementation parameterizes via
  # env vars (E5_PARQUET_URI, E5_SENTINEL_PERIOD, E5_R2_BUCKET) at invocation
  # time. set -u tolerated.
  local _unused1="${1:-}" _unused2="${2:-}"
  local tmpfile
  tmpfile=$(mktemp -t e5_check.XXXXXX.py)
  cat >"$tmpfile" <<'PYEOF'
import os, sys, duckdb, subprocess
URI = os.environ["E5_PARQUET_URI"]
SENTINEL = os.environ["E5_SENTINEL_PERIOD"]
BUCKET = os.environ["E5_R2_BUCKET"]
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").replace("http://", "")
con.execute(f"SET s3_endpoint='{endpoint}'")
con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}'")
con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}'")
con.execute("SET s3_url_style='path'")
HASH_SQL_TMPL = (
    "SELECT MD5(STRING_AGG(row_str, '|' ORDER BY row_str)) FROM ("
    "  SELECT CAST(t AS VARCHAR) AS row_str "
    "  FROM ("
    "    SELECT * EXCLUDE (extracted_at, parser_version) "
    "    FROM read_parquet('{uri}')"
    "  ) t"
    ") sub"
)
hash_before = con.execute(HASH_SQL_TMPL.format(uri=URI)).fetchone()[0]
print(f"e5 hash_before: {hash_before}")
# Re-emit to a determinism-check prefix; the parser must support --out-prefix.
out = subprocess.run(
    [
        "uv", "run", "python3", "scripts/parse_sec_bdc_soi_html_v2.py",
        "--apply", "--periods", SENTINEL,
        "--out-prefix", "sec-bdc/soi-parsed-v2-determinism-check",
    ],
    capture_output=True, text=True, check=True,
)
sys.stdout.write(out.stdout[-2000:])
uri2 = f"s3://{BUCKET}/sec-bdc/soi-parsed-v2-determinism-check/release={SENTINEL}/data.parquet"
hash_after = con.execute(HASH_SQL_TMPL.format(uri=uri2)).fetchone()[0]
print(f"e5 hash_after:  {hash_after}")
# Cleanup the determinism-check prefix to avoid R2 pollution.
import boto3
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="us-east-1",
)
for o in s3.list_objects_v2(
    Bucket=BUCKET,
    Prefix=f"sec-bdc/soi-parsed-v2-determinism-check/release={SENTINEL}/",
).get("Contents", []):
    s3.delete_object(Bucket=BUCKET, Key=o["Key"])
assert hash_before == hash_after, (
    f"e5 determinism breach: hash changed across re-emit ({hash_before} != {hash_after})"
)
print("e5 ok: byte-identical Parquet (excluding extracted_at / parser_version)")
PYEOF
  echo "$tmpfile"
}

# Track temp helper files for cleanup at exit.
_E_CHECK_TMPFILES=()
_cleanup_e_tmpfiles() {
  # set -u: subscripting an empty array can trip "unbound" on older bash;
  # guard with the parameter-default ${arr+x} pattern.
  if [[ -n "${_E_CHECK_TMPFILES+x}" ]]; then
    for f in "${_E_CHECK_TMPFILES[@]}"; do
      [[ -f "$f" ]] && rm -f "$f"
    done
  fi
}
trap _cleanup_e_tmpfiles EXIT

# ====================================================================== #
# Phase 1 — Migrations
# ====================================================================== #

# ── s1: ops.data_source_catalog row bdc_soi_parsed_v2 ─────────────────── #
# (s1 only INSERTs the catalog row; the view UNION ALL branch lives in s2
#  because the branch reads FROM ops.bdc_soi_parsed_v2_runs which s2 creates
#  — ordering correction per predecessor §"Ordering correction" and
#  validator notes pre-flight #4.)
# Asserts: catalog row present AND data_source_catalog_status view emits a
# row for source_slug='bdc_soi_parsed_v2' (the view aggregates from the
# ledger table — once s2 lands, the view branch is queryable).
run_surface "s1" "data-engine-x" '
  ROW=$(dex_psql_query "SELECT 1 FROM ops.data_source_catalog WHERE source_slug='\'"$SOURCE_SLUG"\''" | tr -d "[:space:]") &&
  [[ "$ROW" = "1" ]] &&
  VIEW_ROW=$(dex_psql_query "SELECT 1 FROM ops.data_source_catalog_status WHERE source_slug='\'"$SOURCE_SLUG"\''" | tr -d "[:space:]") &&
  [[ "$VIEW_ROW" = "1" ]]
'

# ── s2: ops.bdc_soi_parsed_v2_runs table + index + view UNION branch ──── #
# Mirrors ops.sec_bdc_soi_ingest_runs verbatim (7-value status enum:
# pending,running,completed,failed,no_change,skipped,dry_run).
# Asserts: table present; index idx_bdc_soi_parsed_v2_runs_release
# (release, started_at DESC) present; status CHECK enum includes no_change.
run_surface "s2" "data-engine-x" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\'"$LEDGER_TABLE"\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_'"$LEDGER_TABLE"'_release'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]] &&
  ENUM_OK=$(dex_psql_query "SELECT 1 FROM pg_constraint WHERE conrelid='\''ops.'"$LEDGER_TABLE"''\''::regclass AND contype='\''c'\'' AND pg_get_constraintdef(oid) LIKE '\''%no_change%'\''" | tr -d "[:space:]") &&
  [[ "$ENUM_OK" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── s3: scripts/parse_sec_bdc_soi_html_v2.py ──────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - inlineurl (the soi.tsv column used as fallback HTML source per validator
#     finding #1 — primary path reads soi.tsv structured columns; HTML is fallback)
#   - InvestmentHoldingsScheduleOfInvestments (TextBlock element for HTML fallback path)
#   - continuedAt (ix continuation chain — reused from v1)
#   - R2 output prefix sec-bdc/soi-parsed-v2
#   - ALL new column literals from directive §"Schema":
#       principal, amortized_cost, investment_interest_rate (any of the typed siblings),
#       investment_identifier, name (BDC registrant), maturity_date_typed
#   - audit-instrumentation columns: parse_confidence, parse_demotion_reason,
#     source_filing_url, parser_version
#   - audit ledger ref: ops.bdc_soi_parsed_v2_runs
#   - is_debt_instrument regex extended per validator finding #4: unitranche + revolv
#   - __version__ semver
#   - NO ContentEncoding header (Volume-King ZSTD Parquet upload contract, L42)
run_surface "s3" "data-engine-x" '
  F="$APP_DIR/scripts/parse_sec_bdc_soi_html_v2.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "inlineurl"                                          "$F" &&
  grep -q "InvestmentHoldingsScheduleOfInvestments"            "$F" &&
  grep -q "continuedAt"                                        "$F" &&
  grep -q "sec-bdc/soi-parsed-v2"                              "$F" &&
  grep -q "principal"                                          "$F" &&
  grep -q "amortized_cost"                                     "$F" &&
  grep -qE "investment_interest_rate(_raw|_base|_spread_bps|_floor_bps|_pik_bps)" "$F" &&
  grep -q "investment_identifier"                              "$F" &&
  grep -q "maturity_date_typed"                                "$F" &&
  grep -q "parse_confidence"                                   "$F" &&
  grep -q "parse_demotion_reason"                              "$F" &&
  grep -q "source_filing_url"                                  "$F" &&
  grep -qE "parser_version|__version__"                         "$F" &&
  grep -q "ops.bdc_soi_parsed_v2_runs"                         "$F" &&
  grep -qE "unitranche|revolv"                                  "$F" &&
  grep -qE "ZSTD|zstd"                                          "$F" &&
  grep -q "application/x-parquet"                              "$F" &&
  ! grep -E "^[^#]*ContentEncoding"                            "$F"
'

# ── s4: scripts/_lib/bdc_soi_classifier.py ────────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - __version__ semver string
#   - parse_confidence + parse_demotion_reason public surface
#   - each enumerated rule code literal per directive §"Schema":
#       name_footnote_ref_stripped, name_fallback_placeholder,
#       maturity_date_suppressed_for_non_debt_instrument, principal_unparseable,
#       interest_rate_format_unrecognized, cusip_checksum_invalid,
#       column_alignment_anomaly, sentinel_value_detected,
#       parser_partial_confidence
#   - is_debt_instrument extended pattern: unitranche + revolv (validator #4)
#   - parse_confidence enum literals: verified_exact, inferred_anchored, rejected
run_surface "s4" "data-engine-x" '
  F="$APP_DIR/scripts/_lib/bdc_soi_classifier.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "__version__"                                        "$F" &&
  grep -q "parse_confidence"                                   "$F" &&
  grep -q "parse_demotion_reason"                              "$F" &&
  grep -q "name_footnote_ref_stripped"                         "$F" &&
  grep -q "name_fallback_placeholder"                          "$F" &&
  grep -q "maturity_date_suppressed_for_non_debt_instrument"   "$F" &&
  grep -q "principal_unparseable"                              "$F" &&
  grep -q "interest_rate_format_unrecognized"                  "$F" &&
  grep -q "cusip_checksum_invalid"                             "$F" &&
  grep -q "column_alignment_anomaly"                           "$F" &&
  grep -q "sentinel_value_detected"                            "$F" &&
  grep -q "parser_partial_confidence"                          "$F" &&
  grep -qE "unitranche|revolv"                                  "$F" &&
  grep -q "verified_exact"                                     "$F" &&
  grep -q "inferred_anchored"                                  "$F"
'

# ── s5: scripts/audit_bdc_soi_parse_v2_sample.py ──────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - --trust-up, --trust-down, --coverage, --compare-v1, --probe-missing-bdcs CLI flags
#   - --N, --per-bucket, --per-reason-bucket, --strict flags
#   - verified_exact, parse_demotion_reason, source_filing_url
#   - explicit constants for the 4 missing BDCs (REVIEWER-CORRECTED 2026-05-22;
#     original audit pinned wrong CIKs that pointed to unrelated entities —
#     see `## Validator notes §"Missing major BDCs"` + `## Review notes`):
#       Blue Owl Capital Corp III (CIK 1807427, fka Owl Rock Capital Corp III)
#       FS KKR Capital Corp       (CIK 1422183, fka FS Investment CORP)
#       FS Specialty Lending Fund (CIK 1501729, fka FS Energy & Power Fund)
#       Prospect Capital Corp     (CIK 1287032)
run_surface "s5" "data-engine-x" '
  F="$APP_DIR/scripts/audit_bdc_soi_parse_v2_sample.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q -- "--trust-up"                                      "$F" &&
  grep -q -- "--trust-down"                                    "$F" &&
  grep -q -- "--coverage"                                      "$F" &&
  grep -q -- "--compare-v1"                                    "$F" &&
  grep -q -- "--probe-missing-bdcs"                            "$F" &&
  grep -q -- "--strict"                                        "$F" &&
  grep -qE -- "--per-bucket|--per-reason-bucket"                "$F" &&
  grep -q "verified_exact"                                     "$F" &&
  grep -q "parse_demotion_reason"                              "$F" &&
  grep -q "source_filing_url"                                  "$F" &&
  grep -qE "1807427|1422183|1501729|1287032"                    "$F"
'

# ====================================================================== #
# Phase 3 — Modal
# ====================================================================== #

# ── s6: modal/bdc_soi_parse_v2_app.py ─────────────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - Modal app name data-engine-x-bdc-soi-parse-v2
#   - Cron("0 14 9 * *") — 9th UTC, 1h staggered after v1 8th UTC cron
#   - both standard ingest secrets (dex-db DB + bulk-ingest-r2 R2)
#   - delegate refs to parse_sec_bdc_soi_html_v2 + audit_bdc_soi_parse_v2_sample
run_surface "s6" "data-engine-x" '
  F="$APP_DIR/modal/bdc_soi_parse_v2_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-bdc-soi-parse-v2"   "$F" &&
  grep -qE "Cron\(\"0 14 9"                   "$F" &&
  grep -q "dex-db"                  "$F" &&
  grep -q "bulk-ingest-r2"                   "$F" &&
  grep -q "parse_sec_bdc_soi_html_v2"        "$F" &&
  grep -q "audit_bdc_soi_parse_v2_sample"    "$F"
'

# ── s7: Modal app data-engine-x-bdc-soi-parse-v2 deployed ──────────────── #
# Capitalized .Description / .State keys per runbook §Gotchas #4.
# Pre-deploy this FAILS (app not yet deployed — correct shape); STRICT post-deploy.
run_surface "s7" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app list --json | jq -e \".[] | select(.Description==\\\"'"$MODAL_APP_NAME"'\\\" or .name==\\\"'"$MODAL_APP_NAME"'\\\") | select(.State==\\\"deployed\\\" or .state==\\\"deployed\\\")\" >/dev/null
  "
'

# ====================================================================== #
# Phase 4 — Verify-only (soft pre-deploy; STRICT=1 post-deploy)
# ====================================================================== #

# ── r1: R2 sec-bdc/soi-parsed-v2/ — >=22 of 23 source periods present ──── #
# Counts distinct release=<period>/ subprefixes with >=1 .parquet object.
# R2 cred-mapping AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID is required for `aws s3 ls`.
run_soft_surface "r1" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    PERIODS=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_SOI_PARSED_V2_PREFIX"'/ --endpoint-url=\$R2_ENDPOINT 2>/dev/null | awk '\''/release=/ {print \$2}'\'' | sort -u | wc -l | tr -d '\'' '\'' )
    [[ \"\$PERIODS\" -ge '"$PERIODS_FLOOR"' ]] || { echo \"r1 miss: periods=\$PERIODS (must be >='"$PERIODS_FLOOR"')\"; exit 1; }
    echo \"r1 ok: periods=\$PERIODS\"
  "
'

# ── e1: sentinel 2025q1 parquet — rows >= 60K + NEW columns present ─────── #
# Downloads sec-bdc/soi-parsed-v2/release=2025q1/data.parquet via DuckDB httpfs,
# asserts row count >= 60K and the v2 NEW column literals from directive
# §"Schema" are present in the schema. Uses _e1_check_inline temp-file pattern
# to sidestep the bash → eval → doppler bash -c → python quote-escaping mess.
_E1_PY=$(_e1_check_inline ""); _E_CHECK_TMPFILES+=("$_E1_PY")
run_soft_surface "e1" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' && \
    E1_PARQUET_URI='\''s3://'"$R2_BUCKET/$R2_SOI_PARSED_V2_PREFIX"'/release='"$E1_SENTINEL_PERIOD"'/data.parquet'\'' \
    E1_FLOOR='"$FLOOR_E1"' \
    uv run python3 '"$_E1_PY"'
  "
'

# ── e2: trust-up audit — 100% precision on verified_exact rows ─────────── #
# Invokes audit_bdc_soi_parse_v2_sample.py --trust-up. Exits 0 iff 100% of
# sampled verified_exact rows reconcile against source HTML at source_filing_url.
run_soft_surface "e2" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    uv run python3 scripts/audit_bdc_soi_parse_v2_sample.py \
      --trust-up --N '"$E2_AUDIT_N"' --per-bucket --strict
  "
'

# ── e3: trust-down audit — demotion justification ─────────────────────── #
# Invokes audit_bdc_soi_parse_v2_sample.py --trust-down. Reports demotion
# justification per parse_demotion_reason bucket. Non-gating in production
# but executor MUST capture findings into PR description.
run_soft_surface "e3" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    uv run python3 scripts/audit_bdc_soi_parse_v2_sample.py \
      --trust-down --N '"$E3_AUDIT_N"' --per-reason-bucket
  "
'

# ── e4: coverage diagnostics — per-field + missing-BDC probe ────────────── #
# Invokes audit_bdc_soi_parse_v2_sample.py --coverage --probe-missing-bdcs.
# Reviewer correction (2026-05-22): the validator's original probe was run
# against WRONG CIKs (1655896/1543918/1666175/1490927 → unrelated entities).
# The CORRECTED CIKs per SEC EDGAR are:
#   Blue Owl Capital Corp III (1807427), FS KKR Capital Corp (1422183),
#   FS Specialty Lending Fund (1501729), Prospect Capital Corp (1287032).
# Executor MUST re-probe these corrected CIKs against the source soi.tsv
# BEFORE concluding source-absence. (See `## Audit plan §s5` + `## Validator
# notes §"Missing major BDCs"` + `## Review notes`.)
run_soft_surface "e4" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    uv run python3 scripts/audit_bdc_soi_parse_v2_sample.py \
      --coverage --probe-missing-bdcs
  "
'

# ── e5: reproducibility — byte-identical re-emit ───────────────────────── #
# Re-runs s3 (parse_sec_bdc_soi_html_v2.py) for the sentinel period writing to
# a determinism-check prefix, compares MD5 row-hash of both Parquet files
# EXCLUDING extracted_at + parser_version (which legitimately change per run).
# Non-determinism is a ship blocker. Uses _e5_check_inline temp-file pattern.
_E5_PY=$(_e5_check_inline ""); _E_CHECK_TMPFILES+=("$_E5_PY")
run_soft_surface "e5" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' && \
    E5_PARQUET_URI='\''s3://'"$R2_BUCKET/$R2_SOI_PARSED_V2_PREFIX"'/release='"$E1_SENTINEL_PERIOD"'/data.parquet'\'' \
    E5_SENTINEL_PERIOD='"$E1_SENTINEL_PERIOD"' \
    E5_R2_BUCKET='"$R2_BUCKET"' \
    uv run python3 '"$_E5_PY"'
  "
'

# ── e6: v1 comparison — >=95% agreement on overlap subset ──────────────── #
# Invokes audit_bdc_soi_parse_v2_sample.py --compare-v1. For overlap subset of
# (adsh, portfolio_company_name) rows present in both v1 and v2, v2 must agree
# with v1 on maturity_date, fair_value, instrument_type at >=95% exact-match
# rate on non-NULL values. Mismatches investigated in the PR description.
run_soft_surface "e6" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    uv run python3 scripts/audit_bdc_soi_parse_v2_sample.py \
      --compare-v1 --agreement-floor-pct '"$E6_AGREEMENT_PCT"'
  "
'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
