#!/usr/bin/env bash
# Stage E (optional/diagnostic): empirical stability check.
#
# Use this on MVs where 01_classify_candidate.sql says time_dependent=false but the
# downstream equality gate keeps failing. Catches non-determinism the regex misses
# (volatile UDFs, xmin leaks, random() in expressions, etc.).
#
# What it does:
#   1. REFRESH MATERIALIZED VIEW CONCURRENTLY <mv>
#   2. Compute hashtext_sum
#   3. Wait 30s
#   4. REFRESH MATERIALIZED VIEW CONCURRENTLY <mv>
#   5. Compute hashtext_sum again
#   6. If hashes differ AND no underlying table writes occurred, MV def is non-deterministic.
#      Force time_dependent=true and re-route the candidate.
#
# Important caveat: if underlying tables receive writes during the 30s window, the second
# hash will legitimately differ even on a fully-deterministic MV def. For high-write MVs,
# either run during a quiet window or interpret the result with that context.
#
# Usage:
#   bash 02_stability_check.sh <schema> <mv_name>
# Example:
#   bash 02_stability_check.sh entities mv_fmcsa_carrier_targeting

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <schema> <mv_name>" >&2
  exit 2
fi

SCHEMA="$1"
MV="$2"
QUALIFIED="${SCHEMA}.${MV}"

echo "stability check: ${QUALIFIED}"
echo "step 1/5: REFRESH MATERIALIZED VIEW CONCURRENTLY ${QUALIFIED}"
doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -q -c 'REFRESH MATERIALIZED VIEW CONCURRENTLY ${QUALIFIED};'"

echo "step 2/5: hash #1"
HASH1=$(doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -t -A -c 'SELECT count(*) || \"|\" || COALESCE(sum(hashtext(t::text))::text, \"NULL\") FROM ${QUALIFIED} t;'")
echo "  ${HASH1}"

echo "step 3/5: sleep 30s"
sleep 30

echo "step 4/5: REFRESH MATERIALIZED VIEW CONCURRENTLY ${QUALIFIED}"
doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -q -c 'REFRESH MATERIALIZED VIEW CONCURRENTLY ${QUALIFIED};'"

echo "step 5/5: hash #2"
HASH2=$(doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -t -A -c 'SELECT count(*) || \"|\" || COALESCE(sum(hashtext(t::text))::text, \"NULL\") FROM ${QUALIFIED} t;'")
echo "  ${HASH2}"

echo
if [ "${HASH1}" = "${HASH2}" ]; then
  echo "STABLE: hashes match across two refreshes 30s apart."
  echo "  treat as time_dependent=false (regex result is correct)."
  exit 0
else
  echo "UNSTABLE: hashes diverged across two refreshes."
  echo "  hash1=${HASH1}"
  echo "  hash2=${HASH2}"
  echo "  treat as time_dependent=true regardless of regex result."
  echo "  re-classify candidate via 01_classify_candidate.sql with override."
  exit 1
fi
