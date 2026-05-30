#!/usr/bin/env bash
# fec_sba_bridge_recall.sh — Verification benchmark for the FEC × SBA employer bridge
#
# Validates that the rewritten bridge (no recall-killing filters) meets
# the acceptance thresholds defined in the /scope directive.
#
# Usage:
#   bash apps/data-engine-x/scripts/benchmarks/fec_sba_bridge_recall.sh /tmp/test_bridge_lance/
#   # or for R2:
#   bash apps/data-engine-x/scripts/benchmarks/fec_sba_bridge_recall.sh \
#     s3://dex-raw-landing-zone/polaris-warehouse/bridges/fec_sba_employer_lance/
#
# Canary join semantics (P5 fix — use name+state, NOT loan_id-based md5):
#   JOIN sba_2025_franchise s ON s.borrname_normalized = b.match_value_normalized
#                             AND s.borrstate = b.match_state
#   NOT loan_id-based (requires decade+locationid unavailable from postgres alone).
#
# Exit codes:
#   0 — all acceptance thresholds pass
#   1 — one or more thresholds FAIL
#   2 — usage error

set -euo pipefail

LANCE_PATH="${1:-}"
if [[ -z "$LANCE_PATH" ]]; then
    echo "Usage: $0 <lance-path>" >&2
    echo "  <lance-path>: local dir (e.g. /tmp/test_bridge_lance/) or S3 URI" >&2
    exit 2
fi

FAIL=0

# Determine DuckDB lance extension loading strategy
# Install once if needed; subsequent runs use the cached extension.
DUCKDB_LANCE_LOAD="INSTALL lance; LOAD lance;"

echo "============================================================"
echo "FEC × SBA Bridge Recall Benchmark"
echo "Target: ${LANCE_PATH}"
echo "============================================================"

# --- 1. Total row count ---
echo ""
echo "-- [1] Total row count (must >= 10,000,000)"
TOTAL_ROWS=$(duckdb -csv -noheader -c \
    "${DUCKDB_LANCE_LOAD} SELECT COUNT(*) FROM lance_scan('${LANCE_PATH}')" 2>/dev/null || \
    python3 -c "import lance; print(lance.dataset('${LANCE_PATH}').count_rows())")
echo "    total_rows = ${TOTAL_ROWS}"
if [[ "$TOTAL_ROWS" -lt 10000000 ]]; then
    echo "    FAIL: total_rows=${TOTAL_ROWS} < 10,000,000"
    FAIL=1
else
    echo "    PASS"
fi

# --- 2. Brand canary counts ---
echo ""
echo "-- [2] Brand canary (strict inequalities vs old Parquet baseline)"
echo "    subway > 597 | burger king > 136 | 7 eleven > 102"

BRAND_QUERY="${DUCKDB_LANCE_LOAD}
SELECT match_value_normalized, COUNT(*) AS n
FROM lance_scan('${LANCE_PATH}')
WHERE match_value_normalized IN ('burger king','mcdonalds','subway','dunkin','seven eleven','7 eleven','mcdonald s','dunkin donuts')
GROUP BY match_value_normalized
ORDER BY n DESC"

BRAND_OUT=$(duckdb -csv -noheader -c "$BRAND_QUERY" 2>/dev/null || echo "duckdb-lance-unavailable")

if [[ "$BRAND_OUT" == "duckdb-lance-unavailable" ]]; then
    # Fallback: use pylance + DuckDB
    BRAND_OUT=$(python3 -c "
import lance, duckdb
ds = lance.dataset('${LANCE_PATH}')
con = duckdb.connect()
con.register('bridge', ds.to_table())
rows = con.execute(\"\"\"
SELECT match_value_normalized, COUNT(*) AS n
FROM bridge
WHERE match_value_normalized IN ('burger king','mcdonalds','subway','dunkin','seven eleven','7 eleven','mcdonald s','dunkin donuts')
GROUP BY match_value_normalized
ORDER BY n DESC
\"\"\").fetchall()
for r in rows: print(f'{r[0]},{r[1]}')
" 2>/dev/null || echo "")
fi

echo "    brand results:"
echo "$BRAND_OUT" | sed 's/^/      /'

SUBWAY_N=$(echo "$BRAND_OUT" | grep "^subway," | cut -d, -f2 || echo "0")
BK_N=$(echo "$BRAND_OUT"     | grep "^burger king," | cut -d, -f2 || echo "0")
SE_N=$(echo "$BRAND_OUT"     | grep "^7 eleven," | cut -d, -f2 || echo "0")
SUBWAY_N="${SUBWAY_N:-0}"; BK_N="${BK_N:-0}"; SE_N="${SE_N:-0}"

if [[ "$SUBWAY_N" -gt 597 ]]; then echo "    subway: PASS (${SUBWAY_N} > 597)"; else echo "    subway: FAIL (${SUBWAY_N} <= 597)"; FAIL=1; fi
if [[ "$BK_N" -gt 136 ]];    then echo "    burger king: PASS (${BK_N} > 136)"; else echo "    burger king: FAIL (${BK_N} <= 136)"; FAIL=1; fi
if [[ "$SE_N" -gt 102 ]];    then echo "    7 eleven: PASS (${SE_N} > 102)"; else echo "    7 eleven: FAIL (${SE_N} <= 102)"; FAIL=1; fi

# --- 3. Tier distribution (silver + rejected must be > 0) ---
echo ""
echo "-- [3] Tier distribution (silver >= 1, rejected >= 1)"

TIER_QUERY="${DUCKDB_LANCE_LOAD}
SELECT confidence_tier, COUNT(*) AS n
FROM lance_scan('${LANCE_PATH}')
GROUP BY confidence_tier
ORDER BY n DESC"

TIER_OUT=$(duckdb -csv -noheader -c "$TIER_QUERY" 2>/dev/null || echo "")

echo "    tier distribution:"
echo "$TIER_OUT" | sed 's/^/      /'

SILVER_N=$(echo "$TIER_OUT"   | grep "^silver,"   | cut -d, -f2 || echo "0")
REJECTED_N=$(echo "$TIER_OUT" | grep "^rejected," | cut -d, -f2 || echo "0")
SILVER_N="${SILVER_N:-0}"; REJECTED_N="${REJECTED_N:-0}"

if [[ "$SILVER_N" -gt 0 ]];   then echo "    silver: PASS (${SILVER_N} rows — arg_max removed)"; else echo "    silver: FAIL (0 — arg_max may not be removed)"; FAIL=1; fi
if [[ "$REJECTED_N" -gt 0 ]]; then echo "    rejected: PASS (${REJECTED_N} rows — collision filter removed)"; else echo "    rejected: FAIL (0 — collision filter may not be removed)"; FAIL=1; fi

# --- 4. Schema superset check ---
echo ""
echo "-- [4] Schema superset (must include all 20 old Parquet columns)"

OLD_COLS="bridge_run_id cmte_id confidence_tier contribution_count cycles_active dataset donor_name donor_occupation donor_state donor_zip fec_employers_at_name_state generated_at loan_id match_method match_state match_value_normalized program sba_borrowers_at_name_state transaction_amt_total bridge_version"

NEW_COLS=$(duckdb -csv -noheader -c \
    "${DUCKDB_LANCE_LOAD} SELECT column_name FROM (DESCRIBE SELECT * FROM lance_scan('${LANCE_PATH}')) ORDER BY 1" \
    2>/dev/null | tr '\n' ' ' || \
    python3 -c "import lance; print(' '.join(sorted(lance.dataset('${LANCE_PATH}').schema.names)))" 2>/dev/null || echo "")

echo "    new schema columns: ${NEW_COLS}"

SCHEMA_FAIL=0
for col in $OLD_COLS; do
    if ! echo "$NEW_COLS" | grep -qw "$col"; then
        echo "    MISSING column: $col"
        SCHEMA_FAIL=1
    fi
done

if [[ "$SCHEMA_FAIL" -eq 0 ]]; then
    echo "    schema: PASS (all 20 required columns present)"
else
    echo "    schema: FAIL"
    FAIL=1
fi

# --- 5. ops.data_sources registration ---
echo ""
echo "-- [5] ops.data_sources registration (format=lance, status=active)"

if command -v doppler &>/dev/null && [[ -f doppler.yaml ]]; then
    DS_CHECK=$(doppler run -- bash -c "psql \"\$DEX_DB_URL_POOLED\" -t -A -c \"
        SELECT format || '|' || status FROM ops.data_sources
         WHERE display_name='fec_sba_employer_lance'\"" 2>/dev/null || echo "")
    if [[ "$DS_CHECK" == "lance|active" ]]; then
        echo "    PASS (fec_sba_employer_lance = lance|active)"
    else
        echo "    NOT YET — registration migration pending (expected post-apply): ${DS_CHECK:-empty}"
        echo "    (This check is informational before --apply; SKIP in pre-apply runs)"
    fi
else
    echo "    SKIP — doppler not available or doppler.yaml not found"
fi

# --- Final verdict ---
echo ""
echo "============================================================"
if [[ "$FAIL" -eq 0 ]]; then
    echo "PASS — all acceptance thresholds met"
    echo "  total_rows=${TOTAL_ROWS}"
    echo "  subway=${SUBWAY_N}, burger_king=${BK_N}, 7_eleven=${SE_N}"
    echo "  silver=${SILVER_N}, rejected=${REJECTED_N}"
else
    echo "FAIL — one or more thresholds not met (see above)"
fi
echo "============================================================"

exit $FAIL
