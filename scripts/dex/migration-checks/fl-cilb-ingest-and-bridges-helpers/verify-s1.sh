#!/usr/bin/env bash
# s1: apps/data-engine-x/modal/fl_cilb_daily_app.py exists in hq-all working tree
# with the load-bearing Modal-native cron decorator + HEAD-probe logic + R2
# upload shape per validator §"Substantive harness refinement findings".
#
# Code-presence + grep-shape check. Actual R2 Parquet landing + audit-ledger
# write is verified in s6 (operator bootstrap).
#
# Validator-confirmed corrections enforced here:
#   - modal-native @app.function(... schedule=modal.Cron("0 11 * * *")) — NOT Trigger.dev
#   - HEAD-probe Last-Modified comparison vs ops.fl_cilb_r2_ingest_runs
#   - ExtraArgs={"ContentType": "application/x-parquet"} ONLY (no ContentEncoding per L42)
#   - writes to ops.fl_cilb_r2_ingest_runs ledger (status strings must match s5 CHECK)
#   - R2 raw landing prefix s3://dex-raw-landing-zone/fl-cilb/release=YYYY-MM-DD/data.parquet
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

TARGET="/Users/benjamincrane/hq-all/apps/data-engine-x/modal/fl_cilb_daily_app.py"

if [[ ! -f "$TARGET" ]]; then
  echo "FAIL: $TARGET missing" >&2
  exit 1
fi

# --- Modal-native cron + app shape (validator §"Modal-native cron vs Trigger.dev") ---
grep -qE "^import modal|^from modal" "$TARGET" || {
  echo "FAIL: 'import modal' missing in $TARGET — directive L51 requires Modal-native cron, not Trigger.dev" >&2
  exit 1
}
grep -qF 'modal.App("data-engine-x-fl-cilb-daily")' "$TARGET" || {
  echo "FAIL: modal.App(\"data-engine-x-fl-cilb-daily\") missing in $TARGET — directive §Deploy targets specifies EXACT app name" >&2
  exit 1
}
grep -q "@app.function" "$TARGET" || {
  echo "FAIL: @app.function decorator missing in $TARGET" >&2
  exit 1
}
grep -qF 'modal.Cron("0 11 * * *")' "$TARGET" || {
  echo "FAIL: modal.Cron(\"0 11 * * *\") schedule literal missing in $TARGET — directive s1 requires 11:00 UTC daily (after FL DBPR ~10:47 UTC refresh)" >&2
  exit 1
}

# --- HEAD-probe for Last-Modified (idempotency basis) ---
grep -qiE "Last-Modified|last_modified" "$TARGET" || {
  echo "FAIL: Last-Modified reference missing in $TARGET — directive s1 requires HEAD-probe comparison vs ops.fl_cilb_r2_ingest_runs.source_observed_at" >&2
  exit 1
}
# HEAD method on upstream CSV URL
grep -qE "method=['\"]HEAD['\"]|requests\.head\(|head\(timeout" "$TARGET" || {
  echo "FAIL: HEAD request idiom missing in $TARGET — directive s1 requires HEAD-probe before download" >&2
  exit 1
}

# --- Upstream CSV URL constant ---
grep -qE "CONSTRUCTIONLICENSE_1\.csv|myfloridalicense\.com/sto/file_download/extracts" "$TARGET" || {
  echo "FAIL: upstream FL DBPR CSV URL missing in $TARGET — must reference https://www2.myfloridalicense.com/sto/file_download/extracts/CONSTRUCTIONLICENSE_1.csv" >&2
  exit 1
}

# --- Audit ledger writes (ops.fl_cilb_r2_ingest_runs) ---
grep -q "ops.fl_cilb_r2_ingest_runs\|fl_cilb_r2_ingest_runs" "$TARGET" || {
  echo "FAIL: ops.fl_cilb_r2_ingest_runs reference missing in $TARGET — directive s1 requires audit-ledger writes" >&2
  exit 1
}

# Status strings written by script — every one MUST appear in s5 CHECK constraint.
# Validator L4: 'pending','running','completed','failed','no_change','skipped'.
for status in "completed" "no_change"; do
  if ! grep -q "['\"]${status}['\"]" "$TARGET"; then
    echo "FAIL: status='$status' literal missing in $TARGET — script must write this status string to ops.fl_cilb_r2_ingest_runs (validator L4: CHECK constraint must cover every status string)" >&2
    exit 1
  fi
done

# --- R2 upload shape (L42 — ContentType only, NO ContentEncoding) ---
grep -qF 'ExtraArgs={"ContentType": "application/x-parquet"}' "$TARGET" || {
  echo "FAIL: ExtraArgs={\"ContentType\": \"application/x-parquet\"} exact literal missing in $TARGET — L42 requires ContentType ONLY (no ContentEncoding)" >&2
  exit 1
}
# Negative grep: ContentEncoding forbidden per L42
if grep -q "ContentEncoding" "$TARGET"; then
  echo "FAIL: ContentEncoding present in $TARGET — L42 violation; CloudFlare R2 + .parquet extension requires ContentType-only header set" >&2
  exit 1
fi

# --- R2 raw landing prefix shape (fl-cilb/release=YYYY-MM-DD/data.parquet) ---
grep -qE "fl-cilb/release=" "$TARGET" || {
  echo "FAIL: R2 prefix 'fl-cilb/release=' missing in $TARGET — directive s1 specifies s3://dex-raw-landing-zone/fl-cilb/release=YYYY-MM-DD/data.parquet" >&2
  exit 1
}

# --- DuckDB transcode shape (per L9 all_varchar=TRUE + ROW_GROUP_SIZE) ---
grep -q "all_varchar" "$TARGET" || {
  echo "FAIL: all_varchar reference missing in $TARGET — L9 requires CSV ingest with all_varchar=TRUE for headerless FL DBPR CSV" >&2
  exit 1
}
grep -qE "FORMAT PARQUET|COMPRESSION ZSTD" "$TARGET" || {
  echo "FAIL: FORMAT PARQUET / COMPRESSION ZSTD reference missing in $TARGET — directive s1 requires DuckDB COPY transcode" >&2
  exit 1
}

echo "s1 OK: $TARGET present; modal-native cron (0 11 * * *) + HEAD-probe + ContentType-only upload + ops.fl_cilb_r2_ingest_runs ledger writes verified"
