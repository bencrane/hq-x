#!/usr/bin/env bash
# s5: after the migration <UTC-TS>_fl_cilb_data_sources_and_bridges.sql is
# applied via scripts/apply_pending_migrations.sh, verify:
#   (a) ops.fl_cilb_r2_ingest_runs table exists (audit ledger for s1).
#   (b) 3 rows in ops.data_sources for the 3 Lance datasets.
#   (c) 2 rows in ops.bridges for the 2 new bridges (s3 + s4).
#   (d) NEGATIVE: NO new row in ops.match_method_versions for
#       legal_name_state_exact_fl v1.0.0 — must remain the publisher PR #467
#       row with input_columns_left={legal_name_normalized,borrstate}
#       (L21 REUSER guard).
#   (e) NEGATIVE: ops.fl_cilb_r2_ingest_runs.status CHECK constraint covers all
#       6 status strings ('pending','running','completed','failed','no_change',
#       'skipped') per L4 / directive s5.
#
# Code-shape pre-check: ALSO verifies the migration FILE exists in hq-all
# working tree with the canonical timestamp-prefix shape per L3 / migrations
# README §"Naming".
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="/Users/benjamincrane/hq-all"

# --- (pre-check) Migration file presence with canonical naming ---
MIG_GLOB="$REPO_ROOT/apps/data-engine-x/supabase/migrations/*_fl_cilb_data_sources_and_bridges.sql"
shopt -s nullglob
mig_files=( $MIG_GLOB )
shopt -u nullglob
if [[ ${#mig_files[@]} -lt 1 ]]; then
  echo "FAIL: no migration file matching '*_fl_cilb_data_sources_and_bridges.sql' under supabase/migrations/ — directive s5 requires <UTC-TS>_fl_cilb_data_sources_and_bridges.sql per L3 / migrations README" >&2
  exit 1
fi
if [[ ${#mig_files[@]} -gt 1 ]]; then
  echo "FAIL: more than one migration file matching '*_fl_cilb_data_sources_and_bridges.sql' (expected exactly 1): ${mig_files[*]}" >&2
  exit 1
fi
MIG_FILE="${mig_files[0]}"
MIG_BASENAME="$(basename "$MIG_FILE")"
# Validate canonical 14-digit UTC timestamp prefix (YYYYMMDDHHMMSS_<slug>.sql)
if ! [[ "$MIG_BASENAME" =~ ^[0-9]{14}_fl_cilb_data_sources_and_bridges\.sql$ ]]; then
  echo "FAIL: migration filename '$MIG_BASENAME' does not match YYYYMMDDHHMMSS_<slug>.sql shape per migrations README §Naming" >&2
  exit 1
fi

# Reject _down.sql sibling (forward-only policy)
if [[ -f "${MIG_FILE%.sql}_down.sql" ]]; then
  echo "FAIL: _down.sql sibling exists ('${MIG_FILE%.sql}_down.sql') — migrations README §Policy is forward-only with git revert rollback; reject" >&2
  exit 1
fi

# --- Migration content greps (idempotent shapes + status CHECK covers all 6) ---
grep -q "CREATE TABLE IF NOT EXISTS ops.fl_cilb_r2_ingest_runs" "$MIG_FILE" || {
  echo "FAIL: CREATE TABLE IF NOT EXISTS ops.fl_cilb_r2_ingest_runs missing in $MIG_FILE — directive s5 requires the audit ledger" >&2
  exit 1
}
# Every status string the s1 script writes MUST appear in CHECK constraint per L4.
for status in pending running completed failed no_change skipped; do
  if ! grep -qE "'$status'" "$MIG_FILE"; then
    echo "FAIL: status '$status' missing from CHECK constraint in $MIG_FILE — L4 / directive s5 requires CHECK (status IN ('pending','running','completed','failed','no_change','skipped'))" >&2
    exit 1
  fi
done

# ON CONFLICT clauses for both ops.data_sources + ops.bridges (idempotent re-application).
grep -q "ON CONFLICT (display_name) DO UPDATE" "$MIG_FILE" || {
  echo "FAIL: ON CONFLICT (display_name) DO UPDATE clause missing in $MIG_FILE — required for ops.data_sources idempotency post-revert" >&2
  exit 1
}
grep -q "ON CONFLICT (bridge_name) DO UPDATE" "$MIG_FILE" || {
  echo "FAIL: ON CONFLICT (bridge_name) DO UPDATE clause missing in $MIG_FILE — required for ops.bridges idempotency post-revert" >&2
  exit 1
}

# Negative: NO INSERT INTO ops.match_method_versions (L21 REUSER guard at SQL layer).
if grep -qE "INSERT INTO ops\.match_method_versions" "$MIG_FILE"; then
  echo "FAIL: INSERT INTO ops.match_method_versions present in $MIG_FILE — L21 violation; reusing publisher PR #467 row; FK lookup-only" >&2
  exit 1
fi

# --- (a) Audit ledger table exists in DB ---
ledger_count=$(dex_psql_query "SELECT count(*) FROM pg_tables WHERE schemaname='ops' AND tablename='fl_cilb_r2_ingest_runs'" | tr -d '[:space:]')
if [[ "$ledger_count" != "1" ]]; then
  echo "FAIL: ops.fl_cilb_r2_ingest_runs table not present in DB (got count='$ledger_count') — migration not applied or table-create failed" >&2
  exit 1
fi

# --- (b) 3 ops.data_sources rows for the 3 Lance datasets ---
ds_count=$(dex_psql_query "SELECT count(*) FROM ops.data_sources WHERE display_name IN ('licensure.fl_cilb_lance','bridges.sba_fl_cilb_lance','bridges.fl_cilb_sunbiz_lance')" | tr -d '[:space:]')
if [[ "$ds_count" != "3" ]]; then
  echo "FAIL: expected 3 ops.data_sources rows for licensure.fl_cilb_lance + bridges.sba_fl_cilb_lance + bridges.fl_cilb_sunbiz_lance; got '$ds_count'" >&2
  exit 1
fi

# --- (c) 2 ops.bridges rows for the 2 new bridges ---
bridges_count=$(dex_psql_query "SELECT count(*) FROM ops.bridges WHERE bridge_name IN ('sba_fl_cilb','fl_cilb_sunbiz')" | tr -d '[:space:]')
if [[ "$bridges_count" != "2" ]]; then
  echo "FAIL: expected 2 ops.bridges rows for bridge_name IN ('sba_fl_cilb','fl_cilb_sunbiz') (NO _lance suffix — naked convention); got '$bridges_count'" >&2
  exit 1
fi

# --- (d) NEGATIVE: legal_name_state_exact_fl v1.0.0 input_columns_left UNCHANGED ---
# Must remain publisher PR #467 sba_sos_fl_owner shape: {legal_name_normalized, borrstate}.
input_cols_left=$(dex_psql_query "SELECT array_to_string(input_columns_left, ',') FROM ops.match_method_versions WHERE match_method_id = (SELECT match_method_id FROM ops.match_methods WHERE method_name = 'legal_name_state_exact_fl') AND semver = '1.0.0'" | tr -d '[:space:]')
case "$input_cols_left" in
  "legal_name_normalized,borrstate"|"borrstate,legal_name_normalized")
    ;;
  *)
    echo "FAIL (L21 REUSER guard): ops.match_method_versions.input_columns_left for (legal_name_state_exact_fl, 1.0.0) is '$input_cols_left' — REUSER corrupted it; must remain publisher PR #467's {legal_name_normalized,borrstate}" >&2
    exit 1
    ;;
esac

# Bridge FK resolution: both new bridges must FK-resolve to legal_name_state_exact_fl.
for bname in sba_fl_cilb fl_cilb_sunbiz; do
  resolved=$(dex_psql_query "SELECT m.method_name FROM ops.bridges b JOIN ops.match_methods m ON m.match_method_id = b.match_method_id WHERE b.bridge_name = '$bname'" | tr -d '[:space:]')
  if [[ "$resolved" != "legal_name_state_exact_fl" ]]; then
    echo "FAIL: ops.bridges.match_method_id FK for bridge_name='$bname' resolves to '$resolved'; expected 'legal_name_state_exact_fl'" >&2
    exit 1
  fi
done

echo "s5 OK: migration file $(basename "$MIG_FILE") present + applied; ops.fl_cilb_r2_ingest_runs ledger table created; 3 ops.data_sources rows + 2 ops.bridges rows; legal_name_state_exact_fl v1.0.0 input_columns_left unchanged (REUSER invariant intact); both bridges FK-resolve to legal_name_state_exact_fl"
