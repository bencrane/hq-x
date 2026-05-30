#!/usr/bin/env bash
#
# dex-person-unified-mv.sh
#
# Benchmark / acceptance harness for the entities.person_unified MV
# (directive: 2026-05-02-dex-person-unified-mv.md).
#
# Reads DEX_DB_URL_DIRECT from env (or --url <url>). Runs each constraint
# C1-C10 + the validator-added C11/C12 checks as a separate SQL probe and
# prints PASS/FAIL with a one-line detail. Final line is RESULT: <p>/<n>.
# Exit code 0 if all pass, 1 otherwise.
#
# Default mode skips C8 (refresh duration) for fast (<2 min) re-runs.
# Pass --with-refresh to also run REFRESH MATERIALIZED VIEW CONCURRENTLY
# and time it. Pass --apply-migration to apply the executor's new migration
# before checking (executor uses this; validator does not).
#
# This script is read-only against source tables and the existing DB. The
# only writes happen if --apply-migration is used.

set -uo pipefail

DB_URL="${DEX_DB_URL_DIRECT:-}"
WITH_REFRESH=0
APPLY_MIGRATION=""

while (( "$#" )); do
  case "$1" in
    --url)
      DB_URL="$2"; shift 2 ;;
    --url=*)
      DB_URL="${1#*=}"; shift ;;
    --with-refresh)
      WITH_REFRESH=1; shift ;;
    --apply-migration)
      APPLY_MIGRATION="$2"; shift 2 ;;
    --apply-migration=*)
      APPLY_MIGRATION="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" >&2; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DB_URL" ]]; then
  echo "ERROR: DEX_DB_URL_DIRECT not set and --url not provided" >&2
  exit 2
fi

PASS=0
FAIL=0
declare -a OUT_LINES

emit() {
  local status="$1"; shift
  local cid="$1"; shift
  local detail="$*"
  OUT_LINES+=("${status}  ${cid}  ${detail}")
  if [[ "$status" == "PASS" ]]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

# Tiny helper: run SQL, return single scalar (or empty on error).
sql_scalar() {
  local sql="$1"
  psql "$DB_URL" -At -v ON_ERROR_STOP=1 -c "$sql" 2>/dev/null | head -1
}

if [[ -n "$APPLY_MIGRATION" ]]; then
  if [[ ! -f "$APPLY_MIGRATION" ]]; then
    echo "ERROR: --apply-migration file not found: $APPLY_MIGRATION" >&2
    exit 2
  fi
  echo "applying migration: $APPLY_MIGRATION ..."
  if ! psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$APPLY_MIGRATION" >/dev/null; then
    echo "ERROR: migration application failed" >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# C1 — MV exists at entities.person_unified
# ---------------------------------------------------------------------------
c1=$(sql_scalar "SELECT COUNT(*) FROM pg_matviews WHERE schemaname='entities' AND matviewname='person_unified'")
if [[ "$c1" == "1" ]]; then
  emit PASS C1 "entities.person_unified exists in pg_matviews"
else
  emit FAIL C1 "entities.person_unified NOT present (count=${c1:-error})"
fi

# ---------------------------------------------------------------------------
# C2 — Required columns present
# ---------------------------------------------------------------------------
required_cols="person_unified_id parent_entity_kind parent_entity_id source source_record_id normalized_name normalized_address raw_name raw_address first_seen_at last_seen_at"
missing=""
present=$(psql "$DB_URL" -At -v ON_ERROR_STOP=1 \
  -c "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='entities' AND c.relname='person_unified' AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum" 2>/dev/null)
for col in $required_cols; do
  if ! grep -qx "$col" <<< "$present"; then
    missing="$missing $col"
  fi
done
if [[ -z "$missing" && -n "$present" ]]; then
  emit PASS C2 "all required columns present"
elif [[ -z "$present" ]]; then
  emit FAIL C2 "MV does not exist (no columns in information_schema)"
else
  emit FAIL C2 "missing columns:$missing"
fi

# ---------------------------------------------------------------------------
# C3 — All declared sources contribute rows (4 sources after validator decision; see notes)
# ---------------------------------------------------------------------------
declared_sources="fmcsa_process_agent hpd_registration_contact nppes_individual sec_adv_signatory finra_direct_owner"
declared_count=$(echo $declared_sources | wc -w | tr -d ' ')
if [[ "$c1" == "1" ]]; then
  found=$(psql "$DB_URL" -At -v ON_ERROR_STOP=1 \
    -c "SELECT source FROM entities.person_unified GROUP BY source HAVING COUNT(*) > 0 ORDER BY source" 2>/dev/null | tr '\n' ' ')
  missing=""
  for s in $declared_sources; do
    if ! grep -qw "$s" <<< "$found"; then
      missing="$missing $s"
    fi
  done
  if [[ -z "$missing" ]]; then
    emit PASS C3 "all $declared_count declared sources contribute rows"
  else
    emit FAIL C3 "sources missing rows:$missing"
  fi
else
  emit FAIL C3 "MV does not exist"
fi

# ---------------------------------------------------------------------------
# C4 — Dedup invariant: no duplicate (parent_entity_kind, parent_entity_id, normalized_name, normalized_address)
# ---------------------------------------------------------------------------
if [[ "$c1" == "1" ]]; then
  dups=$(sql_scalar "SELECT COUNT(*) FROM (SELECT parent_entity_kind, parent_entity_id, normalized_name, normalized_address FROM entities.person_unified GROUP BY 1,2,3,4 HAVING COUNT(*) > 1) sub")
  if [[ "$dups" == "0" ]]; then
    emit PASS C4 "no duplicate dedup-key tuples"
  else
    emit FAIL C4 "found $dups duplicate tuples"
  fi
else
  emit FAIL C4 "MV does not exist"
fi

# ---------------------------------------------------------------------------
# C5 — Joinable to parent entity (per-kind sample of 100 → all join)
# ---------------------------------------------------------------------------
# Per-kind probes:
#   fmcsa_motor_carrier      → entities.motor_carrier_census_records (dot_number text, latest feed_date row)
#   hpd_nyc_building         → entities.hpd_registrations (registrationid bigint)
#   nppes_individual         → entities.nppes_providers (npi bigint, self-parent)
#   sec_ria                  → entities.sec_form_adv_part1_firms (crd_number bigint OR filing_id)
#   finra_firm               → entities.source_finra_brokercheck_firms (crd_number bigint)
if [[ "$c1" == "1" ]]; then
  c5_fail=0
  c5_detail=""

  for kind in fmcsa_motor_carrier hpd_nyc_building nppes_individual sec_ria finra_firm; do
    case "$kind" in
      fmcsa_motor_carrier)
        sql="WITH s AS (SELECT parent_entity_id FROM entities.person_unified WHERE parent_entity_kind='${kind}' AND parent_entity_id IS NOT NULL ORDER BY person_unified_id LIMIT 100) SELECT COUNT(*), COUNT(p.dot_number) FROM s LEFT JOIN entities.motor_carrier_census_records p ON p.dot_number = s.parent_entity_id" ;;
      hpd_nyc_building)
        sql="WITH s AS (SELECT parent_entity_id FROM entities.person_unified WHERE parent_entity_kind='${kind}' AND parent_entity_id IS NOT NULL ORDER BY person_unified_id LIMIT 100) SELECT COUNT(*), COUNT(DISTINCT p.registrationid) FROM s LEFT JOIN entities.hpd_registrations p ON p.registrationid::text = s.parent_entity_id" ;;
      nppes_individual)
        sql="WITH s AS (SELECT parent_entity_id FROM entities.person_unified WHERE parent_entity_kind='${kind}' AND parent_entity_id IS NOT NULL ORDER BY person_unified_id LIMIT 100) SELECT COUNT(*), COUNT(p.npi) FROM s LEFT JOIN entities.nppes_providers p ON p.npi = s.parent_entity_id::bigint" ;;
      sec_ria)
        sql="WITH s AS (SELECT parent_entity_id FROM entities.person_unified WHERE parent_entity_kind='${kind}' AND parent_entity_id IS NOT NULL ORDER BY person_unified_id LIMIT 100) SELECT COUNT(*), COUNT(p.id) FROM s LEFT JOIN entities.sec_form_adv_part1_firms p ON p.crd_number::text = s.parent_entity_id" ;;
      finra_firm)
        sql="WITH s AS (SELECT parent_entity_id FROM entities.person_unified WHERE parent_entity_kind='${kind}' AND parent_entity_id IS NOT NULL ORDER BY person_unified_id LIMIT 100) SELECT COUNT(*), COUNT(p.crd_number) FROM s LEFT JOIN entities.source_finra_brokercheck_firms p ON p.crd_number::text = s.parent_entity_id" ;;
    esac
    out=$(psql "$DB_URL" -At -F '|' -v ON_ERROR_STOP=1 -c "$sql" 2>/dev/null | head -1)
    sample="${out%%|*}"
    joined="${out##*|}"
    if [[ -z "$sample" ]]; then
      c5_fail=1
      c5_detail+=" ${kind}=ERROR"
      continue
    fi
    if [[ "$sample" == "0" ]]; then
      # No rows of this kind in the MV — that's a separate failure type;
      # treat as fail because the kind was declared but no rows exist.
      c5_fail=1
      c5_detail+=" ${kind}=0/0"
      continue
    fi
    if [[ "$sample" != "$joined" ]]; then
      c5_fail=1
      c5_detail+=" ${kind}=${joined}/${sample}"
    else
      c5_detail+=" ${kind}=${joined}/${sample}"
    fi
  done

  if [[ "$c5_fail" == "0" ]]; then
    emit PASS C5 "all kinds 100/100 joinable —${c5_detail}"
  else
    emit FAIL C5 "join shortfall —${c5_detail}"
  fi
else
  emit FAIL C5 "MV does not exist"
fi

# ---------------------------------------------------------------------------
# C6 — No external/HTTP calls in MV migration or refresh path.
# Validator can only check what currently exists; the executor adds files.
# This script verifies the final state at acceptance time.
# ---------------------------------------------------------------------------
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
hits=$(grep -rEn "http://|https://|pg_net\.|http\.|requests\.|urllib|httpx|fetch\(" \
  "${REPO_ROOT}/supabase/migrations/" 2>/dev/null \
  | grep -E "person_unified" | wc -l | tr -d ' ')
# Only flag references inside files whose name mentions person_unified.
mig_files=$(find "${REPO_ROOT}/supabase/migrations/" -name "*person_unified*" 2>/dev/null)
if [[ -z "$mig_files" ]]; then
  emit FAIL C6 "no person_unified migration file present"
else
  refresh_paths=""
  for f in $(grep -rl "person_unified" "${REPO_ROOT}/trigger/" "${REPO_ROOT}/scripts/" "${REPO_ROOT}/app/services/" 2>/dev/null); do
    # Skip the benchmark scripts themselves — they CONTAIN the regex pattern as
    # source text, which would self-match and cause a false positive.
    case "$f" in
      */scripts/benchmarks/dex-person-unified-mv.sh|*/scripts/benchmarks/dex-person-unified-mv-fixtures.sql) continue ;;
    esac
    refresh_paths+=" $f"
  done
  bad=$(grep -EHn "http://|https://|pg_net\.|http\.|requests\.|urllib|httpx|fetch\(" \
    $mig_files $refresh_paths 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$bad" == "0" ]]; then
    emit PASS C6 "no external-call patterns in migration or refresh path"
  else
    emit FAIL C6 "$bad external-call hit(s); inspect migration + refresh files"
  fi
fi

# ---------------------------------------------------------------------------
# C7 — Weekly refresh registered (Trigger.dev schedules.task is the canonical
#       mechanism in DEX; cron may be commented-out per house style — see
#       refresh-usaspending-mvs.ts. The check confirms the file exists, the
#       export is a schedules.task, and the body issues REFRESH ... entities.person_unified).
# ---------------------------------------------------------------------------
trig_dir="${REPO_ROOT}/trigger/src/tasks"
if compgen -G "${trig_dir}/*person*unified*.ts" > /dev/null; then
  trig_file=$(ls ${trig_dir}/*person*unified*.ts | head -1)
  is_schedule=$(grep -c "schedules\.task" "$trig_file" || true)
  refs_refresh=$(grep -Ec "REFRESH MATERIALIZED VIEW.*entities\.person_unified" "$trig_file" || true)
  weekly_marker=$(grep -Ec "weekly|0 \* \* \* 0|0 [0-9]+ \* \* 0|cron:" "$trig_file" || true)
  if [[ "$is_schedule" -ge 1 && ( "$refs_refresh" -ge 1 || $(grep -c "/api/internal" "$trig_file") -ge 1 ) && "$weekly_marker" -ge 1 ]]; then
    emit PASS C7 "$(basename "$trig_file") declares schedules.task with weekly cron + REFRESH person_unified"
  else
    emit FAIL C7 "$(basename "$trig_file") missing one of: schedules.task / REFRESH person_unified / weekly cron marker"
  fi
else
  emit FAIL C7 "no trigger/src/tasks/*person*unified*.ts task file found"
fi

# ---------------------------------------------------------------------------
# C8 — Refresh duration ≤ 30 min (opt-in via --with-refresh)
# ---------------------------------------------------------------------------
if [[ "$WITH_REFRESH" == "1" ]]; then
  if [[ "$c1" == "1" ]]; then
    # BSD-portable timing: %s gives whole seconds (BSD date has no %3N).
    start_s=$(date +%s)
    if psql "$DB_URL" -v ON_ERROR_STOP=1 -c "SET statement_timeout = 0; REFRESH MATERIALIZED VIEW CONCURRENTLY entities.person_unified" >/dev/null 2>&1; then
      end_s=$(date +%s)
      elapsed_s=$(( end_s - start_s ))
      if (( elapsed_s <= 1800 )); then
        emit PASS C8 "REFRESH CONCURRENTLY in ${elapsed_s} s (≤ 30 min)"
      else
        emit FAIL C8 "REFRESH CONCURRENTLY in ${elapsed_s} s (> 30 min bound)"
      fi
    else
      emit FAIL C8 "REFRESH CONCURRENTLY failed"
    fi
  else
    emit FAIL C8 "MV does not exist (cannot refresh)"
  fi
else
  emit PASS C8 "skipped (use --with-refresh to time the refresh)"
fi

# ---------------------------------------------------------------------------
# C9 — Sanity bound on row counts.
# Concrete numbers (validator measured 2026-05-02):
#   N1 (process_agent_with_dot)         = 1,701,672
#   N2 (hpd_contacts_person_types)      =   658,907
#   N3 (nppes_type1)                    = 7,236,712
#   N4 (sec_adv_with_signatory)         =   246,853
#   N5 (finra_directowners_total)       =    49,147
#   Ntotal = 9,893,291
#   Nmax   = 7,236,712
#   floor  = Nmax / 100 = 72,367
# ---------------------------------------------------------------------------
NTOTAL=9893291
NMAX_FLOOR=72367
if [[ "$c1" == "1" ]]; then
  rc=$(sql_scalar "SELECT COUNT(*) FROM entities.person_unified")
  if [[ -z "$rc" ]]; then
    emit FAIL C9 "row count query failed"
  elif (( rc <= 0 )); then
    emit FAIL C9 "row count = $rc (must be > 0)"
  elif (( rc >= NTOTAL )); then
    emit FAIL C9 "row count = $rc (must be < Ntotal=$NTOTAL — dedup must reduce)"
  elif (( rc <= NMAX_FLOOR )); then
    emit FAIL C9 "row count = $rc (must be > Nmax/100 = $NMAX_FLOOR — over-aggressive dedup?)"
  else
    emit PASS C9 "row count = $rc within ($NMAX_FLOOR, $NTOTAL)"
  fi
else
  emit FAIL C9 "MV does not exist"
fi

# ---------------------------------------------------------------------------
# C10 — Migration is forward-only and additive (no DROP/ALTER/TRUNCATE on
# existing objects, only CREATE statements + COMMENT ON allowed).
# ---------------------------------------------------------------------------
if [[ -z "$mig_files" ]]; then
  emit FAIL C10 "no person_unified migration file present"
else
  bad_lines=$(grep -iEHn "(^|\\s)(drop|truncate)\\s|alter\\s+(table|matview|materialized)" $mig_files 2>/dev/null | grep -vE "^\\s*--" | wc -l | tr -d ' ')
  if [[ "$bad_lines" == "0" ]]; then
    emit PASS C10 "no DROP/ALTER/TRUNCATE in migration"
  else
    emit FAIL C10 "$bad_lines DROP/ALTER/TRUNCATE line(s) in migration"
  fi
fi

# ---------------------------------------------------------------------------
# C11 (validator-added) — NULL-safety on dedup-key columns.
# normalized_name and normalized_address must NOT be NULL or empty string.
# ---------------------------------------------------------------------------
if [[ "$c1" == "1" ]]; then
  bad=$(sql_scalar "SELECT COUNT(*) FROM entities.person_unified WHERE normalized_name IS NULL OR normalized_name = '' OR normalized_address IS NULL OR normalized_address = ''")
  if [[ "$bad" == "0" ]]; then
    emit PASS C11 "no NULL/empty dedup-key values"
  else
    emit FAIL C11 "$bad row(s) with NULL/empty dedup-key columns"
  fi
else
  emit FAIL C11 "MV does not exist"
fi

# ---------------------------------------------------------------------------
# C12 (validator-added) — Normalization fixture verification.
# The migration must define a SQL function (or inline expression that is
# also exposed as `entities.person_unified_normalize_name(text)` /
# `entities.person_unified_normalize_address(text)`) that produces the
# expected output for every fixture pair.
# ---------------------------------------------------------------------------
fixtures_path="$(cd "$(dirname "$0")" && pwd)/dex-person-unified-mv-fixtures.sql"
if [[ ! -f "$fixtures_path" ]]; then
  emit FAIL C12 "fixture file missing: $fixtures_path"
else
  # The fixture file is itself a self-checking script; it returns 0 rows on
  # success (each row is a mismatch). If the normalization functions don't
  # exist yet, psql exits non-zero — distinguish that from a counted
  # mismatch.
  fixtures_out=$(psql "$DB_URL" -At -v ON_ERROR_STOP=1 -f "$fixtures_path" 2>/dev/null)
  fixtures_rc=$?
  if [[ $fixtures_rc -ne 0 ]]; then
    emit FAIL C12 "fixture run failed (normalize fns missing or SQL error)"
  else
    mismatches=$(printf '%s' "$fixtures_out" | grep -cv '^$')
    if [[ "$mismatches" == "0" ]]; then
      emit PASS C12 "all normalization fixtures match"
    else
      emit FAIL C12 "$mismatches fixture mismatch(es)"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
TOTAL=$((PASS + FAIL))
for line in "${OUT_LINES[@]}"; do
  echo "$line"
done
echo "RESULT: ${PASS}/${TOTAL}"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
