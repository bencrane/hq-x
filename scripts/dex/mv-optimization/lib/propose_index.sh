#!/usr/bin/env bash
# propose_index.sh — read an EXPLAIN ANALYZE plan, propose an index that targets
# the dominant operator. Heuristics, not a planner; covers ~80% of common cases.
#
# Heuristics:
#   - Seq Scan with Filter on a single equality predicate    → btree on filter col
#   - Sort node feeding a Limit                              → btree on sort cols (limit-tuned)
#   - Hash Join with Seq Scan on inner relation              → btree on join col
#   - Bitmap Heap Scan with high recheck                     → covering index on filter cols
#
# Usage:
#   propose_index.sh --schema entities --mv mv_fmcsa_authority_grants \
#     --canonical "<sql>" --params "100,NULL"
#
# Output (stdout, one of):
#   "OK|btree|<column1>[,<column2>...]"   — proposal made
#   "SKIP|<reason>"                       — no clear win available
#
# Exit codes: 0 = ok or skip (both are normal outputs), 2 = error.

set -euo pipefail

SCHEMA=""
MV=""
CANONICAL=""
PARAMS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --schema)    SCHEMA="$2"; shift 2 ;;
    --mv)        MV="$2"; shift 2 ;;
    --canonical) CANONICAL="$2"; shift 2 ;;
    --params)    PARAMS="$2"; shift 2 ;;
    *) echo "propose_index.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SCHEMA" ] || [ -z "$MV" ] || [ -z "$CANONICAL" ]; then
  echo "propose_index.sh: --schema --mv --canonical all required" >&2
  exit 2
fi

CONN_URL="${DEX_DB_URL_DIRECT:-}"
if [ -z "$CONN_URL" ]; then
  echo "propose_index.sh: \$DEX_DB_URL_DIRECT is empty" >&2
  exit 2
fi

EXEC_LINE="EXECUTE qry"
[ -n "$PARAMS" ] && EXEC_LINE="EXECUTE qry($PARAMS)"

# Get plan as JSON for reliable parsing.
PLAN=$(psql "$CONN_URL" -X -t -A <<EOF 2>&1
PREPARE qry AS $CANONICAL;
EXPLAIN (ANALYZE, TIMING ON, FORMAT JSON) $EXEC_LINE;
DEALLOCATE qry;
EOF
)

if echo "$PLAN" | grep -qE '^ERROR|^FATAL'; then
  echo "SKIP|explain_failed"
  exit 0
fi

# Extract just the JSON portion (psql wraps it; the JSON is the multi-line array).
JSON=$(echo "$PLAN" | sed -n '/^\[/,/^\]/p')

if [ -z "$JSON" ]; then
  echo "SKIP|no_plan_json"
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "SKIP|jq_not_installed"
  exit 0
fi

# Walk the plan looking for nodes that touch our target MV.
# Find the slowest node on the target relation (Actual Total Time).
TARGET_NODES=$(echo "$JSON" | jq -r --arg rel "$MV" '
  [.. | objects | select(.["Relation Name"]? == $rel)]
  | sort_by(-.["Actual Total Time"])
  | .[0] // empty
')

if [ -z "$TARGET_NODES" ]; then
  echo "SKIP|target_mv_not_in_plan"
  exit 0
fi

NODE_TYPE=$(echo "$TARGET_NODES" | jq -r '.["Node Type"] // ""')
FILTER=$(echo "$TARGET_NODES" | jq -r '.["Filter"] // ""')
INDEX_COND=$(echo "$TARGET_NODES" | jq -r '.["Index Cond"] // ""')

case "$NODE_TYPE" in
  "Seq Scan"|"Parallel Seq Scan")
    # Extract column from Filter clause: simplest case is `(col = $N)` or `(col = value)`.
    if [ -n "$FILTER" ]; then
      COL=$(echo "$FILTER" | grep -oE '\b[a-z_][a-z0-9_]*\s*=' | head -1 | sed 's/[ =]//g')
      if [ -n "$COL" ]; then
        echo "OK|btree|${COL}"
        exit 0
      fi
    fi
    echo "SKIP|seq_scan_no_simple_filter"
    ;;
  "Sort")
    # Sort key from plan; parent should be a Limit for this heuristic to fire.
    # Look up the sort key in the node.
    SORT_KEY=$(echo "$TARGET_NODES" | jq -r '.["Sort Key"][]? // ""' | head -1)
    if [ -n "$SORT_KEY" ]; then
      # strip "DESC" / "ASC" / parens
      COL=$(echo "$SORT_KEY" | sed -E 's/[() ]//g; s/(DESC|ASC)$//')
      echo "OK|btree|${COL}"
      exit 0
    fi
    echo "SKIP|sort_no_key"
    ;;
  "Bitmap Heap Scan")
    # If recheck rows >> rows, a covering index could help.
    RECHECK=$(echo "$TARGET_NODES" | jq -r '.["Rows Removed by Index Recheck"] // 0')
    if [ "$RECHECK" -gt 1000 ] 2>/dev/null && [ -n "$FILTER" ]; then
      COL=$(echo "$FILTER" | grep -oE '\b[a-z_][a-z0-9_]*\s*=' | head -1 | sed 's/[ =]//g')
      if [ -n "$COL" ]; then
        echo "OK|btree|${COL}"
        exit 0
      fi
    fi
    echo "SKIP|bitmap_low_recheck"
    ;;
  "Index Scan"|"Index Only Scan")
    # Already index-served. Adding another index unlikely to help.
    echo "SKIP|already_index_served"
    ;;
  *)
    echo "SKIP|unhandled_node_type:${NODE_TYPE}"
    ;;
esac
