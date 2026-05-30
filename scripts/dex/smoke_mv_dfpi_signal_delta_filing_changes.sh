#!/usr/bin/env bash
# Smoke test for entities.mv_dfpi_signal_delta_filing_changes (migration 20260503110000).
# Verifies: existence, column shape, unique index, row sanity, both signal types,
# uniqueness, window correctness, REFRESH CONCURRENTLY works, pg_cron schedule
# registered, and mv_dfpi_franchisors definition unchanged.
#
# Run: ./scripts/smoke_mv_dfpi_signal_delta_filing_changes.sh
# Requires Doppler login pinned to data-engine-x/prd.
set -euo pipefail

run_psql() {
  doppler run -p data-engine-x -c prd -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -tAc \"$1\""
}

echo "==> 1. MV exists in entities schema"
[[ "$(run_psql "SELECT count(*) FROM pg_matviews WHERE schemaname='entities' AND matviewname='mv_dfpi_signal_delta_filing_changes';")" == "1" ]] \
  || { echo "FAIL: MV missing"; exit 1; }

echo "==> 2. Column shape (8 columns, names + types)"
shape="$(run_psql "SELECT string_agg(attname || ':' || format_type(atttypid, atttypmod), ',' ORDER BY attnum) FROM pg_attribute WHERE attrelid='entities.mv_dfpi_signal_delta_filing_changes'::regclass AND attnum > 0 AND NOT attisdropped;")"
expected="org_legal_name_norm:text,signal_type:text,event_date:date,source_table:text,source_row_id:text,dedup_hash:text,payload:jsonb,detected_at:date"
[[ "$shape" == "$expected" ]] \
  || { echo "FAIL: column shape mismatch"; echo "  got:      $shape"; echo "  expected: $expected"; exit 1; }

echo "==> 3. UNIQUE index on dedup_hash exists (required for REFRESH CONCURRENTLY)"
[[ "$(run_psql "SELECT count(*) FROM pg_indexes WHERE schemaname='entities' AND tablename='mv_dfpi_signal_delta_filing_changes' AND indexdef ILIKE 'CREATE UNIQUE INDEX%dedup_hash%';")" == "1" ]] \
  || { echo "FAIL: unique dedup_hash index missing"; exit 1; }

echo "==> 4a. Row count in [1, 250000]"
total="$(run_psql "SELECT count(*) FROM entities.mv_dfpi_signal_delta_filing_changes;")"
[[ "$total" -ge 1 && "$total" -le 250000 ]] \
  || { echo "FAIL: row count out of bounds: $total"; exit 1; }
echo "    total rows: $total"

echo "==> 4b. Both signal types present (filing_new and document_amended)"
filing_new="$(run_psql "SELECT count(*) FROM entities.mv_dfpi_signal_delta_filing_changes WHERE signal_type='filing_new';")"
doc_amended="$(run_psql "SELECT count(*) FROM entities.mv_dfpi_signal_delta_filing_changes WHERE signal_type='document_amended';")"
[[ "$filing_new" -ge 1 && "$doc_amended" -ge 1 ]] \
  || { echo "FAIL: missing signal type (filing_new=$filing_new, document_amended=$doc_amended)"; exit 1; }
echo "    filing_new: $filing_new, document_amended: $doc_amended"

echo "==> 4c. dedup_hash is unique across all rows"
[[ "$(run_psql "SELECT count(*) - count(DISTINCT dedup_hash) FROM entities.mv_dfpi_signal_delta_filing_changes;")" == "0" ]] \
  || { echo "FAIL: dedup_hash duplicates"; exit 1; }

echo "==> 4d. event_date window correctness (no rows older than 91 days)"
[[ "$(run_psql "SELECT count(*) FROM entities.mv_dfpi_signal_delta_filing_changes WHERE event_date < (now() - interval '91 days')::date;")" == "0" ]] \
  || { echo "FAIL: rows older than 91 days exist"; exit 1; }

echo "==> 5. REFRESH MATERIALIZED VIEW CONCURRENTLY succeeds"
doppler run -p data-engine-x -c prd -- bash -c \
  'psql "$DEX_DB_URL_DIRECT" -v ON_ERROR_STOP=1 -c "REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_dfpi_signal_delta_filing_changes;"' >/dev/null

echo "==> 6. pg_cron schedule registered (weekly cadence)"
[[ "$(run_psql "SELECT count(*) FROM cron.job WHERE jobname='refresh_mv_dfpi_signal_delta_filing_changes';")" == "1" ]] \
  || { echo "FAIL: pg_cron schedule missing"; exit 1; }
schedule="$(run_psql "SELECT schedule FROM cron.job WHERE jobname='refresh_mv_dfpi_signal_delta_filing_changes';")"
echo "    schedule: $schedule"

echo "==> 7. mv_dfpi_franchisors definition unchanged (md5 = 7bde29b627f537b2b4d492f0bd68d9bc)"
md5="$(run_psql "SELECT md5(definition) FROM pg_matviews WHERE matviewname='mv_dfpi_franchisors';")"
[[ "$md5" == "7bde29b627f537b2b4d492f0bd68d9bc" ]] \
  || { echo "FAIL: mv_dfpi_franchisors changed (md5 now $md5)"; exit 1; }

echo ""
echo "==> ALL SMOKE CHECKS PASSED"
