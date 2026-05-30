#!/usr/bin/env bash
# test_recipe_shadow_sql.sh — Unit test for Bug 1 fix (stray-semicolon in shadow-build SQL).
#
# Creates a fixture MV in __mv_opt_bench__ on localhost Postgres, fetches its pg_get_viewdef()
# output via the same code path the recipes use (with the trailing-semicolon strip applied),
# then assembles shadow-build SQL for both leaf_swap and subtree_drop_recreate and parse-checks
# each against a local Postgres transaction (BEGIN; <SQL>; ROLLBACK;).
#
# Prerequisite: psql -h localhost -d postgres is reachable.
# Idempotent: fixture schema is dropped on EXIT.
#
# Exit 0 if both recipes' emitted SQL parses cleanly; non-zero otherwise.

set -uo pipefail

PG_CMD=(psql -h localhost -d postgres -X -v ON_ERROR_STOP=1)
FIXTURE_SCHEMA="__mv_opt_bench__"
FIXTURE_MV="mv_bench_fixture"
FIXTURE_FULL="${FIXTURE_SCHEMA}.${FIXTURE_MV}"
SHADOW_MV="${FIXTURE_MV}_v2"
SHADOW_FULL="${FIXTURE_SCHEMA}.${SHADOW_MV}"

PASS=0
FAIL=0

cleanup() {
  "${PG_CMD[@]}" -c "DROP SCHEMA IF EXISTS ${FIXTURE_SCHEMA} CASCADE;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---- Setup fixture ----
"${PG_CMD[@]}" -c "
  DROP SCHEMA IF EXISTS ${FIXTURE_SCHEMA} CASCADE;
  CREATE SCHEMA ${FIXTURE_SCHEMA};
  CREATE MATERIALIZED VIEW ${FIXTURE_FULL} AS
    SELECT
      generate_series(1,10) AS first_col,
      'a'::text AS second_col,
      'b'::text AS third_col,
      'c'::text AS last_col;
  CREATE UNIQUE INDEX ${FIXTURE_MV}_pkey ON ${FIXTURE_FULL} (first_col);
" >/dev/null 2>&1 || {
  echo "FAIL setup: could not create fixture MV ${FIXTURE_FULL}" >&2
  exit 1
}

# ---- Fetch pg_get_viewdef() with the same strip the recipes apply ----
RAW_DEF=$("${PG_CMD[@]}" -t -A -c \
  "SELECT pg_get_viewdef('${FIXTURE_FULL}'::regclass, true);" 2>&1 || true)

if [ -z "$RAW_DEF" ] || echo "$RAW_DEF" | grep -qE '^ERROR|^FATAL'; then
  echo "FAIL setup: pg_get_viewdef returned: ${RAW_DEF}" >&2
  exit 1
fi

# Apply the same strip the recipes now use (Bug 1 fix).
MV_DEF=$(echo "$RAW_DEF" | sed 's/;[[:space:]]*$//')

# Verify the strip actually removed a trailing semicolon (confirms pg_get_viewdef does emit one).
if echo "$RAW_DEF" | grep -qE ';[[:space:]]*$'; then
  echo "  [info] pg_get_viewdef trailing semicolon confirmed present; strip applied." >&2
else
  echo "  [warn] pg_get_viewdef did not have trailing semicolon — test still valid." >&2
fi

UNIQUE_COLS="first_col"

# Bug 1 regression: head -1 must NOT have truncated MV_DEF.
if ! echo "$MV_DEF" | grep -q 'last_col'; then
  echo "FAIL setup: MV_DEF missing 'last_col' — head -1 truncation regressed" >&2
  FAIL=$((FAIL + 1))
else
  echo "  [info] MV_DEF contains 'last_col' (multi-line viewdef captured intact)" >&2
fi

# ---- Verifier helper ----
parse_check() {
  local label="$1"
  local sql="$2"
  local out
  out=$("${PG_CMD[@]}" 2>&1 <<EOF
BEGIN;
${sql}
ROLLBACK;
EOF
) && {
    echo "PASS ${label}: SQL parses cleanly"
    PASS=$((PASS + 1))
  } || {
    echo "FAIL ${label}: SQL parse error — $(echo "$out" | head -2)"
    FAIL=$((FAIL + 1))
  }
}

# ---- leaf_swap shadow-build SQL ----
# Mirrors leaf_swap.sh line 183-184 (and DRY_RUN line 155) after Bug 1 fix:
#   "CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF};"
LEAF_SHADOW_SQL="DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};
CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF};
CREATE UNIQUE INDEX ${SHADOW_MV}_pkey ON ${SHADOW_FULL} (${UNIQUE_COLS});"

parse_check "leaf_swap shadow_build" "$LEAF_SHADOW_SQL"

# ---- subtree_drop_recreate shadow-build SQL ----
# Mirrors subtree_drop_recreate.sh line 228-229 (and DRY_RUN line 203) after Bug 1 fix:
#   "CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF};"
SDR_SHADOW_SQL="DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};
CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF};
CREATE UNIQUE INDEX ${SHADOW_MV}_pkey ON ${SHADOW_FULL} (${UNIQUE_COLS});"

parse_check "subtree_drop_recreate shadow_build" "$SDR_SHADOW_SQL"

# ---- Aggregate ----
echo ""
echo "test_recipe_shadow_sql: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
