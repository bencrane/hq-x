#!/usr/bin/env bash
# Benchmark for Pattern 4 -> Pattern 1 split-count rewrite on
# entities.mv_federal_contract_leads.
#
# Modes:
#   --mode=combined  -> legacy single-query shape (SELECT *, COUNT(*) OVER() ...)
#   --mode=split     -> split pair (SELECT COUNT(*) ... ; SELECT * ...)
#
# Runs 4 invocations: 1 warmup (discarded) + 3 timed. Emits a single JSON
# object on stdout.
#
# Canonical filter mirrors the production-hot pg_stat_statements canonical
# (action_date range + is_first_time_awardee) using a 30-day window inside
# the data range. Exact dates are pinned so results are comparable across
# runs / branches; update if MV ingest shifts the data window.

set -euo pipefail

MODE="combined"
URL="${DEX_DB_URL_POOLED:-}"

# action_date is text in this MV; literals must be 'YYYY-MM-DD' strings.
DATE_FROM="${BENCH_DATE_FROM:-2026-03-02}"
DATE_TO="${BENCH_DATE_TO:-2026-04-01}"
LIMIT="${BENCH_LIMIT:-25}"
OFFSET="${BENCH_OFFSET:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode=*) MODE="${1#--mode=}" ;;
    --url=*)  URL="${1#--url=}" ;;
    --mode)   MODE="$2"; shift ;;
    --url)    URL="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "$URL" ]]; then
  echo "error: DEX_DB_URL_POOLED not set and --url not supplied" >&2
  exit 2
fi
if [[ "$MODE" != "combined" && "$MODE" != "split" ]]; then
  echo "error: --mode must be 'combined' or 'split', got '$MODE'" >&2
  exit 2
fi

TMPDIR_BENCH=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BENCH"' EXIT

run_explain_to_file() {
  # $1 = SQL, $2 = output path
  local sql="$1" out="$2"
  psql "$URL" -X -A -t -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) $sql" > "$out"
}

parse_explain_file() {
  python3 - "$1" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    raw = f.read()
plan = json.loads(raw)[0]
buffers_hit = 0
buffers_read = 0
windowagg = False
scan_node = None
def walk(node):
    global buffers_hit, buffers_read, windowagg, scan_node
    nt = node.get("Node Type", "")
    if "WindowAgg" in nt:
        windowagg = True
    if scan_node is None and ("Scan" in nt):
        idx = node.get("Index Name")
        scan_node = f"{nt} ({idx})" if idx else nt
    buffers_hit += node.get("Shared Hit Blocks", 0) or 0
    buffers_read += node.get("Shared Read Blocks", 0) or 0
    for child in node.get("Plans", []) or []:
        walk(child)
walk(plan["Plan"])
print(json.dumps({
    "exec_ms": plan.get("Execution Time", 0.0),
    "planning_ms": plan.get("Planning Time", 0.0),
    "buffers_hit": buffers_hit,
    "buffers_read": buffers_read,
    "windowagg_present": windowagg,
    "scan_node": scan_node or "unknown",
}))
PY
}

# Page query body (no semicolon — wrapped in EXPLAIN above).
COMBINED_SQL="SELECT *, COUNT(*) OVER() AS total_matched
FROM entities.mv_federal_contract_leads
WHERE action_date >= '${DATE_FROM}'
  AND action_date <= '${DATE_TO}'
  AND is_first_time_awardee = TRUE
ORDER BY action_date DESC
LIMIT ${LIMIT} OFFSET ${OFFSET}"

PAGE_SQL="SELECT *
FROM entities.mv_federal_contract_leads
WHERE action_date >= '${DATE_FROM}'
  AND action_date <= '${DATE_TO}'
  AND is_first_time_awardee = TRUE
ORDER BY action_date DESC
LIMIT ${LIMIT} OFFSET ${OFFSET}"

COUNT_SQL="SELECT COUNT(*)
FROM entities.mv_federal_contract_leads
WHERE action_date >= '${DATE_FROM}'
  AND action_date <= '${DATE_TO}'
  AND is_first_time_awardee = TRUE"

trials=()
warmup=""
windowagg_present=false
scan_node="unknown"

for i in 0 1 2 3; do
  if [[ "$MODE" == "combined" ]]; then
    run_explain_to_file "$COMBINED_SQL" "$TMPDIR_BENCH/run_${i}.json"
    parsed=$(parse_explain_file "$TMPDIR_BENCH/run_${i}.json")
  else
    run_explain_to_file "$PAGE_SQL"  "$TMPDIR_BENCH/run_${i}_page.json"
    run_explain_to_file "$COUNT_SQL" "$TMPDIR_BENCH/run_${i}_count.json"
    page=$(parse_explain_file "$TMPDIR_BENCH/run_${i}_page.json")
    cnt=$(parse_explain_file "$TMPDIR_BENCH/run_${i}_count.json")
    parsed=$(PAGE_JSON="$page" COUNT_JSON="$cnt" python3 -c '
import json, os
page = json.loads(os.environ["PAGE_JSON"])
cnt  = json.loads(os.environ["COUNT_JSON"])
page["count_exec_ms"] = cnt["exec_ms"]
page["count_buffers_hit"] = cnt["buffers_hit"]
page["count_buffers_read"] = cnt["buffers_read"]
print(json.dumps(page))
')
  fi

  if [[ $i -eq 0 ]]; then
    warmup="$parsed"
    windowagg_present=$(WARM="$parsed" python3 -c 'import json,os;print("true" if json.loads(os.environ["WARM"])["windowagg_present"] else "false")')
    scan_node=$(WARM="$parsed" python3 -c 'import json,os;print(json.loads(os.environ["WARM"])["scan_node"])')
    continue
  fi
  trials+=("$parsed")
done

# Compose final JSON.
TRIALS_JSON=$(printf '%s\n' "${trials[@]}" | python3 -c 'import json,sys;print(json.dumps([json.loads(l) for l in sys.stdin if l.strip()]))')

MODE="$MODE" \
DATE_FROM="$DATE_FROM" DATE_TO="$DATE_TO" LIMIT="$LIMIT" OFFSET="$OFFSET" \
TRIALS_JSON="$TRIALS_JSON" \
WINDOWAGG="$windowagg_present" SCAN_NODE="$scan_node" \
python3 -c '
import json, os, statistics
runs = json.loads(os.environ["TRIALS_JSON"])
exec_times = [r["exec_ms"] for r in runs]
out = {
    "mode": os.environ["MODE"],
    "filter": {
        "action_date_from": os.environ["DATE_FROM"],
        "action_date_to": os.environ["DATE_TO"],
        "is_first_time_awardee": True,
        "limit": int(os.environ["LIMIT"]),
        "offset": int(os.environ["OFFSET"]),
    },
    "runs": runs,
    "median_exec_ms": statistics.median(exec_times),
    "stddev_exec_ms": statistics.pstdev(exec_times) if len(exec_times) > 1 else 0.0,
    "windowagg_present": os.environ["WINDOWAGG"] == "true",
    "scan_node": os.environ["SCAN_NODE"],
}
print(json.dumps(out, indent=2))
'
