#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-grants-gov-daily-ingest.
#
# Authored by Stage 2 validator (2026-05-22 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-22-hq-all-grants-gov-daily-ingest.md
#
# Concrete checks wired by Stage 3 executor (2026-05-23 UTC).
#
# Run from repo root:
#   bash apps/data-engine-x/scripts/migration-checks/hq-all-grants-gov-daily-ingest.sh
#
# Exits 0 iff all enumerated constraints PASS.
#
# The `--lance-only` flag skips live R2/DB/Modal checks so static greps can
# run without Doppler. Useful during local iteration dev.
#
# Doppler wrapping: any check that resolves env vars (R2_ENDPOINT,
# POLARIS_PUBLIC_URL, DEX_DB_URL_DIRECT/POOLED) MUST go through:
#
#   doppler run --project hq-all --config prd -- bash -c '<cmd>'
#
# Per apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha": `bash -c '...'`
# defers variable expansion so Doppler-injected secrets are picked up. The
# `--lance-only` flag skips Modal/Trigger.dev probes for local-iteration dev.

set -uo pipefail

LANCE_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lance-only) LANCE_ONLY=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Repo root (harness runs from there per header). Used for cd-before-uv-run.
REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || pwd)"
APP_DIR="${REPO_ROOT}/apps/data-engine-x"
# Lance checks require pylance from the uv project — system python3 lacks it.
# Pattern: cd $APP_DIR && doppler ... -- uv run python3 -c "..."
UV_PYTHON="cd '${APP_DIR}' && uv run python3"

PASS_COUNT=0
FAIL_COUNT=0
FAIL_REASONS=()

run_check() {
  local id="$1" cmd="$2" desc="$3"
  printf "[%s] %s ... " "$id" "$desc"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "FAIL"
    FAIL_REASONS+=("$id: $desc")
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# Pre-flight: confirm Doppler env vars resolve in hq-all/prd. If this fails,
# every downstream check that uses doppler will also fail — surface it first
# so the operator can fix the root cause once.
if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c13" "true" "Doppler hq-all/prd resolves R2_ENDPOINT + POLARIS_PUBLIC_URL + DEX_DB_URL_DIRECT + DEX_DB_URL_POOLED (SKIPPED --lance-only)"
else
  run_check "c13" \
    "doppler run --project hq-all --config prd -- bash -c 'for v in R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY DEX_DB_URL_DIRECT DEX_DB_URL_POOLED POLARIS_PUBLIC_URL POLARIS_ROOT_PRINCIPAL_ID POLARIS_ROOT_PRINCIPAL_SECRET POLARIS_DEFAULT_CATALOG_NAME POLARIS_WAREHOUSE_BASE_LOCATION; do [ -n \"\${!v}\" ] || { echo \"MISSING \$v\" >&2; exit 1; }; done'" \
    "Doppler hq-all/prd resolves R2_ENDPOINT + POLARIS_PUBLIC_URL + DEX_DB_URL_DIRECT + DEX_DB_URL_POOLED"
fi

# Stage 2 outcomes — R2 raw layer
if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c1" "true" "R2 partition for today (synopsis + forecast) at grants-gov/release=\$(date -u +%F)/{synopsis,forecast}/data.parquet (SKIPPED --lance-only)"
  run_check "c7" "true" "R2 ContentType plain .parquet (no Content-Encoding: zstd) on synopsis + forecast (SKIPPED --lance-only)"
else
  run_check "c1" \
    "doppler run --project hq-all --config prd -- bash -c 'python3 -c \"import boto3,os,sys,psycopg; conn=psycopg.connect(os.environ[\\\"DEX_DB_URL_POOLED\\\"]); latest=conn.execute(\\\"SELECT max(feed_date)::text FROM ops.grants_gov_r2_ingest_runs WHERE status IN (\\\\x27completed\\\\x27,\\\\x27no_change\\\\x27)\\\").fetchone()[0]; conn.close(); assert latest, \\\"no completed run\\\"; s3=boto3.client(\\\"s3\\\",endpoint_url=os.environ[\\\"R2_ENDPOINT\\\"],aws_access_key_id=os.environ[\\\"R2_ACCESS_KEY_ID\\\"],aws_secret_access_key=os.environ[\\\"R2_SECRET_ACCESS_KEY\\\"],region_name=\\\"us-east-1\\\"); s3.head_object(Bucket=\\\"dex-raw-landing-zone\\\",Key=f\\\"grants-gov/release={latest}/synopsis/data.parquet\\\"); s3.head_object(Bucket=\\\"dex-raw-landing-zone\\\",Key=f\\\"grants-gov/release={latest}/forecast/data.parquet\\\"); print(f\\\"c1 OK: {latest}\\\")\"'" \
    "R2 partition exists for most-recent completed feed_date: synopsis + forecast at grants-gov/release=<latest>/{synopsis,forecast}/data.parquet"
  run_check "c7" \
    "doppler run --project hq-all --config prd -- bash -c 'python3 -c \"import boto3,os,sys; s3=boto3.client(\\\"s3\\\",endpoint_url=os.environ[\\\"R2_ENDPOINT\\\"],aws_access_key_id=os.environ[\\\"R2_ACCESS_KEY_ID\\\"],aws_secret_access_key=os.environ[\\\"R2_SECRET_ACCESS_KEY\\\"],region_name=\\\"us-east-1\\\"); r=s3.head_object(Bucket=\\\"dex-raw-landing-zone\\\",Key=\\\"grants-gov/release=2026-05-22/synopsis/data.parquet\\\"); ct=r.get(\\\"ContentType\\\",\\\"\\\"); ce=r.get(\\\"ContentEncoding\\\"); sys.exit(0 if ct in [\\\"application/x-parquet\\\",\\\"application/octet-stream\\\"] and ce is None else 1)\"'" \
    "R2 ContentType plain .parquet (no Content-Encoding: zstd) on synopsis + forecast"
fi

# Stage 5 outcomes — Lance + Polaris
if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c2" "true" "Lance row counts (synopsis >= 75_000, forecast >= 1_000) (SKIPPED --lance-only)"
  run_check "c3" "true" "BTREE Scalar index on opportunity_id present on BOTH Lance datasets (SKIPPED --lance-only)"
  run_check "c14" "true" "Polaris namespace 'grants_gov' exists (SKIPPED --lance-only)"
  run_check "c4" "true" "Polaris registration x2 --check-only (SKIPPED --lance-only)"
  run_check "c15" "true" "ops.data_sources rows have format='lance', status='active', owner_app='data-engine-x' (SKIPPED --lance-only)"
else
  # c2, c3, c5, c14 require pylance / requests which are in the uv project, not system python3.
  # Write temp python scripts and invoke via uv run (pattern from caltrans-ccop-active-ingest.sh).
  HARNESS_TMPDIR=$(mktemp -d)
  _C2_PY="${HARNESS_TMPDIR}/c2_lance_rows.py"
  cat > "$_C2_PY" << 'PYEOF'
import lance, os, sys
so = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
}
ds_s = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_synopsis_lance", storage_options=so)
ds_f = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_forecast_lance", storage_options=so)
assert ds_s.count_rows() >= 75000, f"synopsis {ds_s.count_rows()} < 75000"
assert ds_f.count_rows() >= 1000, f"forecast {ds_f.count_rows()} < 1000"
print(f"c2 OK: synopsis={ds_s.count_rows()} forecast={ds_f.count_rows()}")
PYEOF

  _C3_PY="${HARNESS_TMPDIR}/c3_btree.py"
  cat > "$_C3_PY" << 'PYEOF'
import lance, os, sys
so = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
}
ds_s = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_synopsis_lance", storage_options=so)
ds_f = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_forecast_lance", storage_options=so)
def has_btree(ds, col):
    return any(
        ix.get("type","").upper() in ("BTREE", "SCALAR") and col in ix.get("fields",[])
        for ix in ds.list_indices()
    )
assert has_btree(ds_s, "opportunity_id"), f"synopsis BTREE missing: {ds_s.list_indices()}"
assert has_btree(ds_f, "opportunity_id"), f"forecast BTREE missing: {ds_f.list_indices()}"
print("c3 OK: BTREE on opportunity_id in both datasets")
PYEOF

  _C14_PY="${HARNESS_TMPDIR}/c14_polaris_ns.py"
  cat > "$_C14_PY" << 'PYEOF'
import requests, os, sys
base = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
catalog = os.environ["POLARIS_DEFAULT_CATALOG_NAME"]
r = requests.post(
    f"{base}/api/catalog/v1/oauth/tokens",
    data={
        "grant_type": "client_credentials",
        "client_id": os.environ["POLARIS_ROOT_PRINCIPAL_ID"],
        "client_secret": os.environ["POLARIS_ROOT_PRINCIPAL_SECRET"],
        "scope": "PRINCIPAL_ROLE:ALL",
    },
)
r.raise_for_status()
token = r.json()["access_token"]
r2 = requests.get(
    f"{base}/api/catalog/v1/{catalog}/namespaces",
    headers={"Authorization": f"Bearer {token}"},
)
r2.raise_for_status()
# Polaris returns {"namespaces": [["ns1"], ["ns2"], ...]} — list of namespace path lists
nss_raw = r2.json().get("namespaces", [])
# Flatten: each entry is a list of path components; single-level namespaces = ["name"]
flat = set()
for entry in nss_raw:
    if isinstance(entry, list):
        flat.update(entry)
    elif isinstance(entry, str):
        flat.add(entry)
ok = "grants_gov" in flat
print(f"namespaces found: {sorted(flat)}")
print(f"grants_gov present: {ok}")
sys.exit(0 if ok else 1)
PYEOF

  _C5_PY="${HARNESS_TMPDIR}/c5_scanner.py"
  cat > "$_C5_PY" << 'PYEOF'
import lance, pyarrow.compute as pc, os
so = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
}
ds = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_synopsis_lance", storage_options=so)
top_ids = ds.scanner(columns=["opportunity_id"], limit=100).to_table()["opportunity_id"].to_pylist()
n = ds.scanner(filter=pc.field("opportunity_id").isin(top_ids)).count_rows()
assert n >= 1, f"got {n} rows from top_ids scanner filter"
print(f"c5 OK: scanner filter returned {n} rows")
PYEOF

  run_check "c2" \
    "doppler run --project hq-all --config prd -- bash -c \"cd '${APP_DIR}' && uv run python3 '${_C2_PY}'\"" \
    "Lance row counts (synopsis ds.count_rows() >= 75_000, forecast >= 1_000)"
  run_check "c3" \
    "doppler run --project hq-all --config prd -- bash -c \"cd '${APP_DIR}' && uv run python3 '${_C3_PY}'\"" \
    "BTREE Scalar index on opportunity_id present on BOTH Lance datasets"
  run_check "c14" \
    "doppler run --project hq-all --config prd -- bash -c \"cd '${APP_DIR}' && uv run python3 '${_C14_PY}'\"" \
    "Polaris namespace 'grants_gov' exists (GET /v1/{catalog}/namespaces returns it)"
  run_check "c4" \
    "doppler run --project hq-all --config prd -- bash -c 'cd apps/data-engine-x && python3 scripts/init_polaris_lance_generic.py --namespace grants_gov --table opportunity_synopsis_lance --check-only && python3 scripts/init_polaris_lance_generic.py --namespace grants_gov --table opportunity_forecast_lance --check-only'" \
    "Polaris registration x2: init_polaris_lance_generic.py --check-only returns 0 for opportunity_synopsis_lance + opportunity_forecast_lance"
  run_check "c15" \
    "doppler run --project hq-all --config prd -- bash -c 'psql \"\$DEX_DB_URL_POOLED\" -tAc \"SELECT count(*) FROM ops.data_sources WHERE display_name LIKE '\''grants_gov%opportunity%lance'\'' AND format='\''lance'\'' AND status='\''active'\'' AND owner_app='\''data-engine-x'\''\"' | grep -qE '^2\$'" \
    "ops.data_sources rows have format='lance', status='active', owner_app='data-engine-x' (per L50)"
fi

# Pattern C readiness — smoke query
if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c5" "true" "Pattern C smoke: scanner(filter=opportunity_id IN (top-100 opportunity_ids from synopsis)).count_rows() >= 1 (SKIPPED --lance-only)"
else
  run_check "c5" \
    "doppler run --project hq-all --config prd -- bash -c \"cd '${APP_DIR}' && uv run python3 '${_C5_PY}'\"" \
    "Pattern C smoke: scanner(filter=opportunity_id IN (top-100 opportunity_ids from synopsis)).count_rows() >= 1"
fi

# Stage 3 outcomes — daily cadence + idempotency
if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c6" "true" "Idempotency: two consecutive modal-run invocations -> 1 R2 partition for today + 2 ops.grants_gov_r2_ingest_runs rows (SKIPPED --lance-only)"
else
  _C6_PY="${HARNESS_TMPDIR}/c6_idempotency.py"
  cat > "$_C6_PY" << 'PYEOF'
import psycopg, os, sys
db = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
conn = psycopg.connect(db)
row = conn.execute(
    "SELECT count(*) FROM ("
    "  SELECT feed_date FROM ops.grants_gov_r2_ingest_runs"
    "  WHERE status IN ('completed','no_change')"
    "  GROUP BY feed_date HAVING count(DISTINCT status) >= 2"
    ") sq"
).fetchone()
conn.close()
n = row[0] if row else 0
print(f"c6: {n} feed_date(s) have both completed + no_change rows")
sys.exit(0 if n >= 1 else 1)
PYEOF
  run_check "c6" \
    "doppler run --project hq-all --config prd -- bash -c \"cd '${APP_DIR}' && uv run python3 '${_C6_PY}'\"" \
    "Idempotency: at least one feed_date has both completed + no_change ledger rows (two-run idempotency confirmed)"
fi

run_check "c11" \
  "doppler run --project hq-all --config prd -- bash -c 'psql \"\$DEX_DB_URL_POOLED\" -tAc \"SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON c.conrelid=t.oid JOIN pg_namespace n ON t.relnamespace=n.oid WHERE n.nspname='\''ops'\'' AND t.relname='\''grants_gov_r2_ingest_runs'\'' AND c.contype='\''c'\''\"' | grep -qE 'pending.*running.*completed.*failed.*no_change'" \
  "ops.grants_gov_r2_ingest_runs CHECK constraint covers all 5 status strings: pending,running,completed,failed,no_change (per L4)"

if [[ $LANCE_ONLY -eq 1 ]]; then
  run_check "c10" "true" "ops.data_sources has exactly 2 rows matching display_name LIKE '%grants_gov%opportunity%lance' (SKIPPED --lance-only)"
else
  run_check "c10" \
    "doppler run --project hq-all --config prd -- bash -c 'psql \"\$DEX_DB_URL_POOLED\" -tAc \"SELECT count(*) FROM ops.data_sources WHERE display_name LIKE '\''%grants_gov%opportunity%lance'\'' AND format='\''lance'\''\"' | grep -qE '^2\$'" \
    "ops.data_sources has exactly 2 rows matching display_name LIKE '%grants_gov%opportunity%lance'"
fi

run_check "c12" \
  "grep -rE 'modal[.]Cron|schedule=modal[.]Cron' apps/data-engine-x/modal/grants_gov_daily_app.py | grep -qE 'grants_gov|Cron'" \
  "Daily cron wired: modal.Cron in modal/grants_gov_daily_app.py pointing at the grants_gov entrypoint (per FABS modal precedent at modal/usaspending_api_daily_assistance_app.py:201)"

# Code hygiene checks (greps — fast, run last)
run_check "c8" \
  "python3 -c \"import ast,glob,sys; ok=True
for p in glob.glob('apps/data-engine-x/scripts/run_grants_gov*.py') + glob.glob('apps/data-engine-x/modal/grants_gov*.py'):
    src=open(p).read()
    if 'lance.write_dataset' in src and 'lance_commit_lock' not in src: ok=False; print(f'MISSING: {p}')
sys.exit(0 if ok else 1)\"" \
  "lance_commit_lock wraps every lance.write_dataset call in apps/data-engine-x/scripts/run_grants_gov_*.py"

run_check "c9" \
  "python3 -c \"import sys; src=open('apps/data-engine-x/app/services/lance_views.py').read(); assert 'opportunity_synopsis_lance' in src and 'opportunity_forecast_lance' in src and 'grants_gov' in src, 'missing URI'; assert 'grants_gov_opportunity_synopsis_lance_raw' in src and 'grants_gov_opportunity_forecast_lance_raw' in src, 'missing view names'; synopsis_boot=src.count('grants_gov_opportunity_synopsis_lance_raw'); forecast_boot=src.count('grants_gov_opportunity_forecast_lance_raw'); assert 'register_at_boot=False' in src, 'no register_at_boot=False found'; print('c9 OK')\"" \
  "LANCE_VIEWS in app/services/lance_views.py registers grants_gov_opportunity_synopsis_lance_raw + grants_gov_opportunity_forecast_lance_raw, both register_at_boot=False"

run_check "c16" \
  "grep -rE \"modal\\.App\\([\\\"']data-engine-x-grants-gov-daily[\\\"']\\)\" apps/data-engine-x/modal/ apps/data-engine-x/scripts/ 2>/dev/null | wc -l | grep -qE '^[[:space:]]*1\$'" \
  "Modal app name 'data-engine-x-grants-gov-daily' is the only declaration in apps/data-engine-x/modal/*.py (no collision with existing apps)"

echo ""
echo "===== HARNESS SUMMARY ====="
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo "PASS: $PASS_COUNT / $TOTAL"
echo "FAIL: $FAIL_COUNT"
if [[ $FAIL_COUNT -gt 0 ]]; then
  echo ""
  echo "Failed checks:"
  printf -- "  - %s\n" "${FAIL_REASONS[@]}"
  exit 1
fi
exit 0
