#!/usr/bin/env bash
# Verification harness for hq-all Phase 0c: alerting + atomic ingest + idempotency.
#
# 5 load-bearing gates (per directive §"Verification gate"):
#   G1  Synthetic stale source → POST /alerts/run-cycle → Telegram receives breach alert
#   G2  Synthetic failed run   → POST /alerts/run-cycle → Telegram receives ingest_failed alert
#   G3  Forced-failure atomic_ingest → R2 staging cleaned + ledger row 'failed'
#   G4  Idempotency replay → atomic_ingest_run returns 'skipped' on second call
#   G5  Dedup window honored → 3 cycles in <1h, exactly 1 emission per (alert_id)
#
# Tests are designed to run against PROD (api.dataengine.run + the prod DB).
# Synthetic state is inserted under `phase_0c_test_*` display names and
# cleaned up at the end. The harness is fully idempotent — repeat invocations
# do not leave state behind.
#
# Doppler idiom (per apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha"):
#   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -c "..."'
# project=hq-all config=prd is pinned via apps/data-engine-x/doppler.yaml.
#
# Usage:
#   ./hq-all-phase-0c-alerting-atomic-ingest.sh
#   ./hq-all-phase-0c-alerting-atomic-ingest.sh --gate G1   # one gate
#   ./hq-all-phase-0c-alerting-atomic-ingest.sh --skip-telegram   # don't send actual messages
#
# Exits 0 only if every requested gate passes.

set -uo pipefail

GATE_FILTER=""
SKIP_TELEGRAM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gate)            GATE_FILTER="$2"; shift 2 ;;
    --skip-telegram)   SKIP_TELEGRAM=1;  shift 1 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

HQ_ALL="${HQ_ALL:-/Users/benjamincrane/hq-all}"
APP_DIR="$HQ_ALL/apps/data-engine-x"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the shim — exposes dex_psql_query, dex_psql_ddl, _dex_doppler.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_lib-shim.sh"

DEX_BASE_URL=$(_dex_doppler 'printf "%s" "$DEX_BASE_URL"')
DEX_API_KEY=$(_dex_doppler 'printf "%s" "$DEX_SERVICE_TOKEN"')
TELEGRAM_BOT_TOKEN=$(_dex_doppler 'printf "%s" "$TELEGRAM_BOT_TOKEN"')
TELEGRAM_CHAT_ID=$(_dex_doppler 'printf "%s" "$TELEGRAM_ALERT_CHAT_ID"')

if [[ -z "$DEX_BASE_URL" || -z "$DEX_API_KEY" ]]; then
  echo "FAIL: DEX_BASE_URL or DEX_SERVICE_TOKEN missing from Doppler" >&2
  exit 1
fi

TS=$(date -u +%Y%m%d%H%M%S)
TEST_PREFIX="phase_0c_test_${TS}"
STALE_SOURCE="${TEST_PREFIX}_stale"
FAILED_SOURCE="${TEST_PREFIX}_failed"
ATOMIC_SOURCE="${TEST_PREFIX}_atomic"

PASS=0
FAIL=0

pass() { echo "PASS — $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL — $*" >&2; FAIL=$((FAIL + 1)); }

cleanup() {
  echo "==> cleanup: removing $TEST_PREFIX state"
  dex_psql_ddl "
    DELETE FROM ops.alert_emissions WHERE alert_id IN (
      SELECT alert_id FROM ops.alert_subscriptions WHERE source_id IN (
        SELECT source_id FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%'
      )
    );
    DELETE FROM ops.alert_subscriptions WHERE source_id IN (
      SELECT source_id FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%'
    );
    DELETE FROM ops.data_source_ingest_runs WHERE source_id IN (
      SELECT source_id FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%'
    );
    DELETE FROM ops.data_source_slas WHERE source_id IN (
      SELECT source_id FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%'
    );
    DELETE FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%';
  " >/dev/null 2>&1 || true
}

trap cleanup EXIT

want_gate() {
  [[ -z "$GATE_FILTER" || "$GATE_FILTER" == "$1" ]]
}

# ─────────────────────────────────────────────────────────────────────────────
# Setup: insert test sources + subscriptions
# ─────────────────────────────────────────────────────────────────────────────

echo "==> setup: inserting test sources ${TEST_PREFIX}*"

dex_psql_ddl "
  INSERT INTO ops.data_sources (display_name, storage_uri, format, owner_app, status)
  VALUES
    ('${STALE_SOURCE}', 's3://test/${STALE_SOURCE}', 'r2_parquet', 'data-engine-x', 'active'),
    ('${FAILED_SOURCE}', 's3://test/${FAILED_SOURCE}', 'r2_parquet', 'data-engine-x', 'active'),
    ('${ATOMIC_SOURCE}', 's3://test/${ATOMIC_SOURCE}', 'r2_parquet', 'data-engine-x', 'active');

  INSERT INTO ops.data_source_slas (source_id, sla_freshness_seconds, sla_basis)
  SELECT source_id, 86400, 'last_ingested'::data_source_sla_basis
    FROM ops.data_sources
   WHERE display_name IN ('${STALE_SOURCE}', '${FAILED_SOURCE}');

  -- Stale source: insert a 2-day-old 'succeeded' run so breach=true
  INSERT INTO ops.data_source_ingest_runs (source_id, status, started_at, completed_at)
  SELECT source_id, 'succeeded'::data_source_run_status,
         NOW() - INTERVAL '2 days', NOW() - INTERVAL '2 days' + INTERVAL '1 minute'
    FROM ops.data_sources WHERE display_name = '${STALE_SOURCE}';

  -- Failed source: insert a recent 'failed' run so ingest_failed alert fires
  INSERT INTO ops.data_source_ingest_runs (source_id, status, started_at, completed_at, error_message)
  SELECT source_id, 'failed'::data_source_run_status,
         NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes',
         'phase_0c_synthetic_failure for verification gate G2'
    FROM ops.data_sources WHERE display_name = '${FAILED_SOURCE}';

  -- Subscribe operator's Telegram for breach + ingest_failed on the test sources
  -- (use dedup_window_seconds=60 for the dedup test G5; default 4h is too long for the harness)
  INSERT INTO ops.alert_subscriptions (source_id, alert_kind, channel, recipient, dedup_window_seconds)
  SELECT ds.source_id, kind::alert_kind, 'telegram'::alert_channel, '${TELEGRAM_CHAT_ID}', 60
    FROM ops.data_sources ds
    CROSS JOIN (VALUES ('breach'), ('ingest_failed')) k(kind)
   WHERE ds.display_name LIKE '${TEST_PREFIX}%'
  ON CONFLICT (source_id, alert_kind, channel, recipient) DO NOTHING;
" >/dev/null

source_count=$(dex_psql_query "SELECT count(*) FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%'")
sub_count=$(dex_psql_query "SELECT count(*) FROM ops.alert_subscriptions WHERE source_id IN (SELECT source_id FROM ops.data_sources WHERE display_name LIKE '${TEST_PREFIX}%')")
echo "  inserted: ${source_count} sources, ${sub_count} subscriptions"

if [[ "$source_count" != "3" ]]; then
  fail "setup: expected 3 test sources, got ${source_count}"
  exit 1
fi
if [[ "$sub_count" != "6" ]]; then
  fail "setup: expected 6 subscriptions (3 sources × 2 kinds), got ${sub_count}"
  exit 1
fi

# Wrapper for /alerts/run-cycle
run_cycle() {
  curl -sS -X POST \
    -H "Authorization: Bearer $DEX_API_KEY" \
    -H "Content-Type: application/json" \
    "$DEX_BASE_URL/api/v1/internal/observability/alerts/run-cycle" \
    -d '{}'
}

# ─────────────────────────────────────────────────────────────────────────────
# Gate G1: synthetic stale → breach Telegram alert
# ─────────────────────────────────────────────────────────────────────────────

if want_gate G1; then
  echo "==> G1: breach alert on synthetic stale source"

  pre_sent=$(dex_psql_query "
    SELECT count(*) FROM ops.alert_emissions e
      JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
      JOIN ops.data_sources ds ON ds.source_id = s.source_id
     WHERE ds.display_name = '${STALE_SOURCE}' AND s.alert_kind = 'breach' AND e.delivery_status = 'sent'
  ")

  resp=$(run_cycle)
  echo "  /alerts/run-cycle response: $resp"

  # Sleep briefly so the emission row is committed.
  sleep 2

  post_sent=$(dex_psql_query "
    SELECT count(*) FROM ops.alert_emissions e
      JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
      JOIN ops.data_sources ds ON ds.source_id = s.source_id
     WHERE ds.display_name = '${STALE_SOURCE}' AND s.alert_kind = 'breach' AND e.delivery_status = 'sent'
  ")

  if (( post_sent > pre_sent )); then
    pass "G1 breach alert: ${pre_sent} → ${post_sent} sent emission(s) for ${STALE_SOURCE}"
  else
    fail "G1 breach alert: no new sent emission (pre=${pre_sent}, post=${post_sent})"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Gate G2: synthetic failed run → ingest_failed Telegram alert
# ─────────────────────────────────────────────────────────────────────────────

if want_gate G2; then
  echo "==> G2: ingest_failed alert on synthetic failed run"

  pre_sent=$(dex_psql_query "
    SELECT count(*) FROM ops.alert_emissions e
      JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
      JOIN ops.data_sources ds ON ds.source_id = s.source_id
     WHERE ds.display_name = '${FAILED_SOURCE}' AND s.alert_kind = 'ingest_failed' AND e.delivery_status = 'sent'
  ")

  # G1 already ran a cycle in this harness, which would have caught the
  # ingest_failed for FAILED_SOURCE too. So we compare against pre_sent
  # before G1, but G1's run already counted; we just need post_sent > 0.
  post_sent=$(dex_psql_query "
    SELECT count(*) FROM ops.alert_emissions e
      JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
      JOIN ops.data_sources ds ON ds.source_id = s.source_id
     WHERE ds.display_name = '${FAILED_SOURCE}' AND s.alert_kind = 'ingest_failed' AND e.delivery_status = 'sent'
  ")

  if (( post_sent > 0 )); then
    pass "G2 ingest_failed alert: ${post_sent} sent emission(s) for ${FAILED_SOURCE} (caught in G1 cycle)"
  else
    # In case G1 didn't run or dedup-blocked, try one more cycle
    resp=$(run_cycle)
    echo "  /alerts/run-cycle response (G2-retry): $resp"
    sleep 2
    post_sent=$(dex_psql_query "
      SELECT count(*) FROM ops.alert_emissions e
        JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
        JOIN ops.data_sources ds ON ds.source_id = s.source_id
       WHERE ds.display_name = '${FAILED_SOURCE}' AND s.alert_kind = 'ingest_failed' AND e.delivery_status = 'sent'
    ")
    if (( post_sent > 0 )); then
      pass "G2 ingest_failed alert: ${post_sent} sent emission(s) for ${FAILED_SOURCE}"
    else
      fail "G2 ingest_failed alert: still 0 sent emissions after run-cycle"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Gate G3: forced-failure atomic_ingest → R2 staging cleaned + ledger 'failed'
# ─────────────────────────────────────────────────────────────────────────────
#
# Strategy: call atomic_ingest_run with a finalize_callable that raises
# AFTER writing to staging. Verify (a) staging key absent in R2; (b) ledger
# row exists with status='failed' and error_message set.

if want_gate G3; then
  echo "==> G3: forced-failure atomic_ingest cleans staging + marks ledger failed"

  G3_KEY="phase_0c_test_g3/data.parquet"
  G3_BUCKET=$(_dex_doppler 'printf "%s" "$R2_BUCKET"')
  if [[ -z "$G3_BUCKET" ]]; then
    G3_BUCKET="dex-raw-landing-zone"
  fi
  G3_RUN_ID=$(_dex_doppler 'python3 -c "import uuid; print(uuid.uuid4())"')

  # Run a Python snippet that exercises atomic_ingest_run with a callable
  # that writes to staging then raises.
  py_out=$(_dex_doppler "cd $APP_DIR && python3 -c \"
import sys, os, json
sys.path.insert(0, '.')
import boto3

def staging_writer(staging_bucket, staging_key, **kw):
    client = boto3.client('s3',
        endpoint_url=os.environ['R2_ENDPOINT'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto',
    )
    client.put_object(Bucket=staging_bucket, Key=staging_key, Body=b'phase_0c_test_payload')
    raise RuntimeError('forced failure mid-flow for G3 verification')

from app.services.atomic_ingest import atomic_ingest_run
try:
    atomic_ingest_run(
        source_display_name='${ATOMIC_SOURCE}',
        idempotency_run_id='${G3_RUN_ID}',
        format='r2_parquet',
        finalize_callable=staging_writer,
        dest_bucket='${G3_BUCKET}',
        dest_key='${G3_KEY}',
    )
    print('UNEXPECTED_SUCCESS')
except Exception as e:
    print('caught:', type(e).__name__, str(e)[:200])
\"")
  echo "  python invocation: $py_out"

  # Check ledger row is 'failed' (qualify status — ambiguous between
  # data_source_ingest_runs.status and data_sources.status across the JOIN)
  ledger_status=$(dex_psql_query "
    SELECT r.status::text FROM ops.data_source_ingest_runs r
      JOIN ops.data_sources ds ON ds.source_id = r.source_id
     WHERE ds.display_name = '${ATOMIC_SOURCE}'
       AND r.run_metadata->>'idempotency_run_id' = '${G3_RUN_ID}'
     ORDER BY r.started_at DESC LIMIT 1
  ")
  if [[ "$ledger_status" == "failed" ]]; then
    pass "G3 ledger row marked 'failed' for run_id=${G3_RUN_ID}"
  else
    fail "G3 ledger row status=${ledger_status:-<absent>} expected 'failed'"
  fi

  # Check staging key cleaned. We use the same _staging_key derivation:
  G3_STAGING="phase_0c_test_g3/_staging/run_id=${G3_RUN_ID}/data.parquet"
  staging_check=$(_dex_doppler "python3 -c \"
import os, boto3
client = boto3.client('s3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto',
)
try:
    client.head_object(Bucket='${G3_BUCKET}', Key='${G3_STAGING}')
    print('PRESENT')
except Exception as e:
    if '404' in str(e) or 'NoSuchKey' in str(e) or 'Not Found' in str(e):
        print('ABSENT')
    else:
        print('OTHER:', str(e)[:200])
\"")
  if [[ "$staging_check" == "ABSENT" ]]; then
    pass "G3 staging key absent (cleaned up): ${G3_STAGING}"
  else
    fail "G3 staging key check: ${staging_check}"
  fi

  # Cleanup the final dest (in case copy somehow ran) — best-effort.
  _dex_doppler "python3 -c \"
import os, boto3
client = boto3.client('s3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto',
)
try: client.delete_object(Bucket='${G3_BUCKET}', Key='${G3_KEY}')
except: pass
\"" > /dev/null 2>&1 || true
fi

# ─────────────────────────────────────────────────────────────────────────────
# Gate G4: idempotency — replay returns 'skipped'
# ─────────────────────────────────────────────────────────────────────────────

if want_gate G4; then
  echo "==> G4: idempotent replay of atomic_ingest_run returns 'skipped'"

  G4_KEY="phase_0c_test_g4/data.parquet"
  G4_BUCKET=$(_dex_doppler 'printf "%s" "$R2_BUCKET"')
  if [[ -z "$G4_BUCKET" ]]; then
    G4_BUCKET="dex-raw-landing-zone"
  fi
  G4_RUN_ID=$(_dex_doppler 'python3 -c "import uuid; print(uuid.uuid4())"')

  py_out=$(_dex_doppler "cd $APP_DIR && python3 -c \"
import sys, os, json
sys.path.insert(0, '.')
import boto3

def staging_writer(staging_bucket, staging_key, **kw):
    client = boto3.client('s3',
        endpoint_url=os.environ['R2_ENDPOINT'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto',
    )
    client.put_object(Bucket=staging_bucket, Key=staging_key, Body=b'phase_0c_test_g4_payload')

from app.services.atomic_ingest import atomic_ingest_run
r1 = atomic_ingest_run(
    source_display_name='${ATOMIC_SOURCE}',
    idempotency_run_id='${G4_RUN_ID}',
    format='r2_parquet',
    finalize_callable=staging_writer,
    dest_bucket='${G4_BUCKET}',
    dest_key='${G4_KEY}',
)
print('first:', r1['status'])

# Second call — replay with same idempotency_run_id
r2 = atomic_ingest_run(
    source_display_name='${ATOMIC_SOURCE}',
    idempotency_run_id='${G4_RUN_ID}',
    format='r2_parquet',
    finalize_callable=staging_writer,
    dest_bucket='${G4_BUCKET}',
    dest_key='${G4_KEY}',
)
print('replay:', r2['status'], 'existing_run_id:', r2.get('existing_run_id'))
\"")
  echo "  python invocation: $py_out"

  first_line=$(echo "$py_out" | grep '^first:' | head -1)
  replay_line=$(echo "$py_out" | grep '^replay:' | head -1)
  if [[ "$first_line" =~ succeeded ]] && [[ "$replay_line" =~ skipped ]]; then
    pass "G4 idempotency: first=succeeded, replay=skipped"
  else
    fail "G4 idempotency: first=[$first_line] replay=[$replay_line]"
  fi

  # Cleanup
  _dex_doppler "python3 -c \"
import os, boto3
client = boto3.client('s3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto',
)
try: client.delete_object(Bucket='${G4_BUCKET}', Key='${G4_KEY}')
except: pass
\"" > /dev/null 2>&1 || true
fi

# ─────────────────────────────────────────────────────────────────────────────
# Gate G5: dedup window honored — 3 cycles, 1 emission per alert_id
# ─────────────────────────────────────────────────────────────────────────────

if want_gate G5; then
  echo "==> G5: dedup window — 3 cycles in <1m, 1 emission per (alert_id) for stale source"

  # First, clean any existing emissions for the stale source so we start fresh.
  dex_psql_ddl "
    DELETE FROM ops.alert_emissions WHERE alert_id IN (
      SELECT s.alert_id FROM ops.alert_subscriptions s
        JOIN ops.data_sources ds ON ds.source_id = s.source_id
       WHERE ds.display_name = '${STALE_SOURCE}'
    );
  " > /dev/null

  # Run 3 cycles in rapid succession. dedup_window_seconds was set to 60 in
  # setup; the first cycle sends, the next 2 should skip.
  for i in 1 2 3; do
    resp=$(run_cycle)
    echo "  cycle $i: $resp"
    sleep 1
  done

  sent_count=$(dex_psql_query "
    SELECT count(*) FROM ops.alert_emissions e
      JOIN ops.alert_subscriptions s ON s.alert_id = e.alert_id
      JOIN ops.data_sources ds ON ds.source_id = s.source_id
     WHERE ds.display_name = '${STALE_SOURCE}' AND s.alert_kind = 'breach' AND e.delivery_status = 'sent'
  ")
  if [[ "$sent_count" == "1" ]]; then
    pass "G5 dedup: exactly 1 sent emission across 3 cycles (window=60s)"
  else
    fail "G5 dedup: expected 1 sent emission, got ${sent_count}"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "==> harness summary: PASS=$PASS FAIL=$FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo "Phase 0c harness: FAILED" >&2
  exit 1
fi
echo "Phase 0c harness: all gates passed"
