#!/usr/bin/env bash
# benchmark.sh — run a parameterized canonical query 5 times against a target relation, return p50.
#
# Wraps PREPARE/EXECUTE/DEALLOCATE so $N placeholders resolve. Includes one warmup run
# (discarded) before the 5 measurement runs to absorb the cold-cache outlier.
#
# Usage:
#   p50=$(bash benchmark.sh --canonical "<sql>" --params "100,NULL" --conn DEX_DB_URL_DIRECT)
#
# Env:
#   The connection-url env var (default DEX_DB_URL_DIRECT) must inject via doppler.
#   Always invoke under `doppler run -- bash benchmark.sh ...`.
#
# Output (stdout): single number — p50 in milliseconds.
# Exit codes: 0 = ok, 1 = canonical parse error, 2 = execution error.

set -euo pipefail

CANONICAL=""
PARAMS=""
CONN_VAR="DEX_DB_URL_DIRECT"

while [ $# -gt 0 ]; do
  case "$1" in
    --canonical) CANONICAL="$2"; shift 2 ;;
    --params)    PARAMS="$2"; shift 2 ;;
    --conn)      CONN_VAR="$2"; shift 2 ;;
    *) echo "benchmark.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$CANONICAL" ]; then
  echo "benchmark.sh: --canonical required" >&2
  exit 2
fi

CONN_URL="${!CONN_VAR:-}"
if [ -z "$CONN_URL" ]; then
  echo "benchmark.sh: \$$CONN_VAR is empty (run under doppler)" >&2
  exit 2
fi

# Build PREPARE/EXECUTE block. If PARAMS given, use EXECUTE qry($PARAMS); else EXECUTE qry.
EXEC_LINE="EXECUTE qry"
if [ -n "$PARAMS" ]; then
  EXEC_LINE="EXECUTE qry($PARAMS)"
fi

# Warmup + 5 runs. Capture Execution Time per run.
RUNS=$(psql "$CONN_URL" -X -t -A <<EOF 2>&1
PREPARE qry AS $CANONICAL;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT TEXT) $EXEC_LINE;
DEALLOCATE qry;
EOF
)

if echo "$RUNS" | grep -qE '^ERROR|^FATAL'; then
  echo "benchmark.sh: psql error" >&2
  echo "$RUNS" >&2
  exit 1
fi

# Extract execution times. Skip the first (warmup); take the next 5.
TIMES=$(echo "$RUNS" | awk '/Execution Time:/ {print $3}' | tail -n +2 | head -5)

if [ "$(echo "$TIMES" | wc -l | tr -d ' ')" -lt 5 ]; then
  echo "benchmark.sh: got fewer than 5 measurement runs" >&2
  echo "$RUNS" >&2
  exit 1
fi

# p50 = median = 3rd value of sorted 5
echo "$TIMES" | sort -n | awk 'NR==3 {print}'
