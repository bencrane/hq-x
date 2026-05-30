#!/usr/bin/env bash
# modal-quality-score.sh — regenerate apps/data-engine-x/modal/QUALITY_SCORE.md
# from live ledger + heartbeat data + repo introspection.
#
# Closes P1-3 from the 2026-05-25 systemic Modal critique (audit §"P1-3").
# Without longitudinal portfolio grades, the operator cannot answer "is the
# fleet getting healthier or sicker week-over-week." This script computes
# per-app + per-layer A-D grades and writes them deterministically.
#
# Per-app grade (last 30 days of bulk_ingest.feed_ingest_runs):
#   A  — 100% non-failed outcomes, >=1 successful run
#   B  — >=95% non-failed
#   C  — >=80% non-failed
#   D  — <80% non-failed OR zero runs in window
#
# Per-layer grade (portfolio-wide structural checks):
#   secret_hygiene       — `fmcsa-ingest-db` references swept (A iff zero refs)
#   ledger_correctness   — 3 USAspending sisters use canonical landing.ledger (A iff all 3)
#   retry_policy         — every @app.function has # retry-policy: tag (A iff ratchet pass)
#   observability        — heartbeat rows landed for >5min orchestrators in last 7d
#   test_coverage        — count of test_modal_* files / target
#   pattern_adherence    — apps map to documented patterns (manual; defaults to A)
#
# Runs weekly via launchd (~/Desktop/hq/launchd/modal-quality-score-weekly.plist).
#
# Manual invocation:
#   ./apps/data-engine-x/scripts/modal-quality-score.sh
#   → regenerates apps/data-engine-x/modal/QUALITY_SCORE.md
#   → exit 0 on success; non-zero if write fails

# Lenient error handling: the script must complete even if pieces of the
# DB query fail (e.g., heartbeat table empty, doppler unavailable). The
# generated markdown carries placeholders for missing data.
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "FATAL: not in a git repo" >&2; exit 2;
}
DEX_DIR="$REPO_ROOT/apps/data-engine-x"
MODAL_DIR="$DEX_DIR/modal"
OUTPUT="$MODAL_DIR/QUALITY_SCORE.md"
TESTS_DIR="$DEX_DIR/tests"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

run_query() {
    # Usage: run_query "SELECT ..." — uses dex-db pooled URL via Doppler.
    doppler run --project hq-all --config prd -- \
        bash -c "psql -tA -F'|' \"\$DEX_DB_URL_POOLED\" -c \"$1\"" 2>/dev/null
}

count_modal_apps() {
    find "$MODAL_DIR" -maxdepth 1 -name "*.py" -not -name "__*" | wc -l | tr -d ' '
}

count_fmcsa_ingest_db_refs() {
    # Only count ACTUAL secret bindings, not documentation/comment mentions.
    grep -rE "modal\.Secret\.from_name\(['\"]fmcsa-ingest-db['\"]\)" "$DEX_DIR" 2>/dev/null \
        | wc -l | tr -d ' '
}

ratchet_test_passes() {
    # Returns 0 iff retry+secret ratchet tests pass.
    ( cd "$DEX_DIR" && python3 -m pytest \
        tests/test_modal_retry_audit.py \
        tests/test_modal_secrets_scoped.py \
        tests/test_modal_ledger_helper.py \
        -q >/dev/null 2>&1 )
}

# ---------------------------------------------------------------------------
# Per-app grade query — outcomes from last 30 days.
# ---------------------------------------------------------------------------
APP_GRADES_QUERY="
WITH stats AS (
    SELECT
        source_id || ':' || feed_name AS app_id,
        COUNT(*) AS runs,
        COUNT(*) FILTER (
            WHERE outcome NOT IN ('failed', 'failed_orchestrator_crashed',
                                  'failed_upstream_error', 'failed_db_error',
                                  'failed_r2_error', 'failed_unknown')
        ) AS non_failed,
        AVG(duration_seconds)::int AS avg_dur_s,
        MAX(started_at) AS last_run_at
    FROM bulk_ingest.feed_ingest_runs
    WHERE started_at > NOW() - INTERVAL '30 days'
      AND is_dry_run = FALSE
      -- Retired sources: a deleted ingest leaves its final failed runs in the
      -- ledger, which otherwise score a phantom D for 30 days (zero non-failed)
      -- until they age out. Exclude them here. Add a source_id when an ingest is
      -- retired; the canonical successor keeps reporting under its own source_id.
      --   ny_openbook_vendor_payments: retired 2026-05-18 in the OpenBookNY ->
      --   data.ny.gov Socrata pivot (PR #502, 62512d23). The OSC ColdFusion
      --   portal rejected automated downloads (parse_failure); superseded by
      --   ny_data_construction_vendor_payments (rb9h-9fit), which is healthy.
      AND source_id NOT IN ('ny_openbook_vendor_payments')
    GROUP BY app_id
)
SELECT
    app_id,
    runs,
    non_failed,
    CASE
        WHEN runs = 0 THEN 'D'
        WHEN non_failed * 100 / runs >= 100 THEN 'A'
        WHEN non_failed * 100 / runs >= 95  THEN 'B'
        WHEN non_failed * 100 / runs >= 80  THEN 'C'
        ELSE 'D'
    END AS grade,
    avg_dur_s,
    last_run_at
FROM stats
ORDER BY app_id
"

# ---------------------------------------------------------------------------
# Heartbeat freshness for the observability layer grade.
# ---------------------------------------------------------------------------
HEARTBEAT_QUERY="
SELECT
    cron_app,
    COUNT(DISTINCT run_id) AS runs_with_heartbeats,
    MAX(heartbeat_at) AS last_heartbeat
FROM ops.cron_heartbeats
WHERE heartbeat_at > NOW() - INTERVAL '7 days'
GROUP BY cron_app
ORDER BY cron_app
"

# ---------------------------------------------------------------------------
# Collect data.
# ---------------------------------------------------------------------------
NOW_UTC="$(date -u +'%Y-%m-%d %H:%M UTC')"
TOTAL_APPS="$(count_modal_apps)"
FMCSA_REFS="$(count_fmcsa_ingest_db_refs)"

APP_ROWS="$(run_query "$APP_GRADES_QUERY" || echo "")"
HEARTBEAT_ROWS="$(run_query "$HEARTBEAT_QUERY" || echo "")"

# ---------------------------------------------------------------------------
# Compute per-layer grades.
# ---------------------------------------------------------------------------
secret_hygiene_grade() {
    if [[ "$FMCSA_REFS" -eq 0 ]]; then
        echo "A"
    elif [[ "$FMCSA_REFS" -lt 5 ]]; then
        echo "B"
    elif [[ "$FMCSA_REFS" -lt 20 ]]; then
        echo "C"
    else
        echo "D"
    fi
}

ledger_correctness_grade() {
    # A iff the 3 USAspending sisters import from landing.ledger (no inline _record_run).
    local count
    count="$(grep -l "from landing.ledger import" \
        "$MODAL_DIR/usaspending_api_daily_app.py" \
        "$MODAL_DIR/usaspending_api_daily_assistance_app.py" \
        "$MODAL_DIR/usaspending_api_daily_contracts_lance_app.py" 2>/dev/null | wc -l | tr -d ' ')"
    case "$count" in
        3) echo "A" ;;
        2) echo "B" ;;
        1) echo "C" ;;
        *) echo "D" ;;
    esac
}

retry_policy_grade() {
    if ratchet_test_passes; then echo "A"; else echo "D"; fi
}

observability_grade() {
    local hb_apps
    hb_apps="$(echo "$HEARTBEAT_ROWS" | grep -c "^data-engine-x-" || echo 0)"
    if [[ "$hb_apps" -ge 3 ]]; then echo "A"
    elif [[ "$hb_apps" -ge 1 ]]; then echo "B"
    else echo "D"
    fi
}

test_coverage_grade() {
    local test_count
    test_count="$(find "$TESTS_DIR" -maxdepth 1 -name "test_modal_*.py" 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$test_count" -ge 4 ]]; then echo "A"
    elif [[ "$test_count" -ge 2 ]]; then echo "B"
    elif [[ "$test_count" -ge 1 ]]; then echo "C"
    else echo "D"
    fi
}

SECRET_HYGIENE="$(secret_hygiene_grade)"
LEDGER_CORRECTNESS="$(ledger_correctness_grade)"
RETRY_POLICY="$(retry_policy_grade)"
OBSERVABILITY="$(observability_grade)"
TEST_COVERAGE="$(test_coverage_grade)"

# ---------------------------------------------------------------------------
# Render markdown.
# ---------------------------------------------------------------------------
{
cat <<HEADER
# Modal portfolio — quality score

**Auto-generated by** \`scripts/modal-quality-score.sh\` — do not hand-edit.
**Last regenerated:** $NOW_UTC
**Total Modal apps in repo:** $TOTAL_APPS

Per the 2026-05-25 systemic Modal critique (audit §"P1-3"), this scoreboard
gives the operator portfolio-level visibility into whether the cron fleet
is getting healthier or sicker week-over-week. Grades are A–D, deterministic
from the latest ledger + heartbeat + repo state. No LLM in the loop.

D-grades that persist across multiple regenerations are real signal worth
investigating.

## Per-layer grades

| Layer | Grade | Signal |
|---|---|---|
| Secret hygiene | $SECRET_HYGIENE | $FMCSA_REFS \`fmcsa-ingest-db\` references remaining (A iff 0) |
| Ledger correctness | $LEDGER_CORRECTNESS | 3 USAspending sisters using canonical landing.ledger helper (A iff all 3) |
| Retry policy | $RETRY_POLICY | \`pytest test_modal_retry_audit\` + secrets + ledger ratchets (A iff all pass) |
| Observability | $OBSERVABILITY | distinct cron_apps with heartbeats in last 7 days (A iff >=3) |
| Test coverage | $TEST_COVERAGE | tests/test_modal_*.py files (A iff >=4) |
| Pattern adherence | A | Manual — apps map to Pattern A/B/C per DATA-FACTORY-ARCHITECTURE-PATTERNS.md |

## Per-app grades (last 30 days, is_dry_run=FALSE)

| App (source_id : feed_name) | Runs | Non-failed | Grade | Avg duration (s) | Last run |
|---|---|---|---|---|---|
HEADER

if [[ -z "$APP_ROWS" ]]; then
    echo "| _no production runs in last 30 days_ | — | — | — | — | — |"
else
    while IFS='|' read -r app_id runs non_failed grade avg_dur last_run; do
        [[ -z "$app_id" ]] && continue
        echo "| \`$app_id\` | $runs | $non_failed | $grade | $avg_dur | $last_run |"
    done <<< "$APP_ROWS"
fi

cat <<FOOTER

## Heartbeat coverage (last 7 days)

| cron_app | runs with heartbeats | last heartbeat |
|---|---|---|
FOOTER

if [[ -z "$HEARTBEAT_ROWS" ]]; then
    echo "| _no heartbeat rows in last 7 days_ | — | — |"
else
    while IFS='|' read -r cron_app n_runs last_hb; do
        [[ -z "$cron_app" ]] && continue
        echo "| \`$cron_app\` | $n_runs | $last_hb |"
    done <<< "$HEARTBEAT_ROWS"
fi

cat <<EOFMD

---

## How this is computed

- **Per-app grade** — query: \`bulk_ingest.feed_ingest_runs\` last 30 days, \`is_dry_run = FALSE\`. Non-failed = outcome NOT IN the 5 \`failed_*\` labels. A=100%, B>=95%, C>=80%, else D. Zero runs → D (cron not firing or app retired).
- **Secret hygiene** — \`grep -r "fmcsa-ingest-db" apps/data-engine-x/\` count. A iff 0.
- **Ledger correctness** — count of 3 USAspending sisters that import \`from landing.ledger import\`. A iff all 3.
- **Retry policy** — \`pytest test_modal_retry_audit.py test_modal_secrets_scoped.py test_modal_ledger_helper.py\` exits 0.
- **Observability** — distinct \`cron_app\` values in \`ops.cron_heartbeats\` in last 7 days. A iff >=3.
- **Test coverage** — count of \`tests/test_modal_*.py\` files. A iff >=4.
- **Pattern adherence** — manual; defaults A. Update if apps drift from documented patterns.

Persistent D-grades trigger a \`/scope\` cycle to remediate.

EOFMD
} > "$OUTPUT"

echo "✓ regenerated $OUTPUT"
