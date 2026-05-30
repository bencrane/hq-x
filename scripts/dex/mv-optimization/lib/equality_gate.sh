#!/usr/bin/env bash
# equality_gate.sh — compute the (count, hashtext_sum) tuple for a relation.
#
# Two callers compose this into a gate:
#   1. Capture before:  before=$(equality_gate.sh entities.mv_x)
#   2. Capture after:   after=$(equality_gate.sh entities.mv_x_v2)
#   3. Compare:         [ "$before" = "$after" ] && echo PASS || echo FAIL
#
# For time-aligned strategies (CURRENT_DATE in def), capture before and after must happen
# back-to-back to minimize drift. The orchestrator handles ordering.
#
# Usage:
#   tuple=$(bash equality_gate.sh <schema.relation>)
#
# Output (stdout): "<count>|<hashtext_sum>" on one line.
# Exit codes: 0 = ok, 1 = relation not found, 2 = arg error.

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "equality_gate.sh: usage: $0 <schema.relation>" >&2
  exit 2
fi

REL="$1"
CONN_URL="${DEX_DB_URL_DIRECT:-}"
if [ -z "$CONN_URL" ]; then
  echo "equality_gate.sh: \$DEX_DB_URL_DIRECT is empty (run under doppler)" >&2
  exit 2
fi

OUT=$(psql "$CONN_URL" -X -t -A -c \
  "SELECT count(*) || '|' || COALESCE(sum(hashtext(t::text))::text, 'NULL') FROM $REL t;" 2>&1)

if echo "$OUT" | grep -qE '^ERROR|^FATAL'; then
  echo "equality_gate.sh: psql error on $REL" >&2
  echo "$OUT" >&2
  exit 1
fi

echo "$OUT" | tr -d ' \n'
echo  # newline at end
