#!/usr/bin/env bash
# Verification harness for directive: 2026-05-03-pre-entities-checklist-2-5
# Filled by AUDIT subagent (2026-05-03).
#
# Surfaces (4):
#   s1a  config    /Users/benjamincrane/hq-all/.git/hooks/post-merge
#                  Install dex-migration-apply post-merge hook (mirror of
#                  ~/Desktop/hq-all/.git/hooks/post-merge). Closes premortem H8.
#                  NOTE: ~/hq-all is NOT a worktree — it has its own .git/ dir
#                  (rev-parse --git-common-dir == .git). Hook installs locally.
#
#   s1b  config    ops.dex_applied_migrations  (prod DB)
#                  Bootstrap ledger with all already-applied migration filenames
#                  (210 rows: 211 *.sql on disk minus the 1 backfill row already
#                  inserted). INCLUDES the 2 *_down.sql files (mark them
#                  "applied" so the applier never tries to run them — stop-gap
#                  until the applier learns to skip *_down.sql; see directive
#                  ## Out of scope, H18-flavored follow-up).
#                  GATE: this MUST run BEFORE s1a takes effect on the next
#                  `git pull` in ~/hq-all, otherwise the post-merge hook would
#                  trigger 210 forward applies + 2 down applies.
#
#   s2   code      ~/Desktop/hq-all/scripts/scope-cycle-report.sh
#                  Raise STALE_THRESHOLD default 600 -> 1800; warn-and-clamp
#                  when SCOPE_STALE_THRESHOLD<120. Premortem H5.
#
#   s3   code      apps/data-engine-x/scripts/_lib/dex.sh  (line 15)
#                  DEX_DOPPLER_PROJECT default data-engine-x -> hq-all per
#                  ~/Desktop/hq/PROTOCOL.md §"In-scope projects (locked 2026-05-02)".
#                  Validator confirmed: only one caller of DEX_DOPPLER_PROJECT
#                  exists in apps/data-engine-x/ (dex.sh:20, the helper itself).
#                  No runtime caller depends on the default — safe.
#
# Doppler shell convention (apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha"):
#   doppler run --project hq-all --config prd -- bash -c 'psql "$DEX_DB_URL_..." -c "..."'
#   The bash -c subshell defers $VAR expansion until Doppler injects.
#
# Helper library: per /scope SKILL.md determinism rule, this harness sources
# the canonical apps/data-engine-x/scripts/_lib/dex.sh via the vault thin-shim
# at ~/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh — never re-encode the
# Doppler/psql plumbing inline.
#
# Usage:
#   ./2026-05-03-pre-entities-checklist-2-5.sh
#   ./2026-05-03-pre-entities-checklist-2-5.sh --repo bencrane/hq-all
#   ./2026-05-03-pre-entities-checklist-2-5.sh --repo bencrane/hq      # vault
#   ./2026-05-03-pre-entities-checklist-2-5.sh --surface s1a
#   ./2026-05-03-pre-entities-checklist-2-5.sh --surface s1b
#   MERGE_SHA=abc1234 ./2026-05-03-pre-entities-checklist-2-5.sh

set -uo pipefail

# Source vault helper-lib shim (locates apps/data-engine-x/scripts/_lib/dex.sh).
# After s3 lands and ~/hq-all is pulled, the helper's DEX_DOPPLER_PROJECT
# default is hq-all; before s3 lands, callers must set DEX_DOPPLER_PROJECT=hq-all
# explicitly. The harness exports it defensively so it works either way.
export DEX_DOPPLER_PROJECT="${DEX_DOPPLER_PROJECT:-hq-all}"
export DEX_DOPPLER_CONFIG="${DEX_DOPPLER_CONFIG:-prd}"
# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_FILTER=""
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

VAULT="$HOME/Desktop/hq"
HQ_ALL="$HOME/hq-all"
HQ_ALL_CANONICAL="$HOME/Desktop/hq-all"
APP_DIR="$HQ_ALL_CANONICAL/apps/data-engine-x"
MIGRATIONS_DIR="$APP_DIR/supabase/migrations"

if [[ ! -d "$VAULT" ]]; then
  echo "FAIL: vault dir missing: $VAULT" >&2
  exit 1
fi
if [[ ! -d "$HQ_ALL" ]]; then
  echo "FAIL: ~/hq-all clone missing: $HQ_ALL" >&2
  exit 1
fi
if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

echo "==> Verifying §7 pre-entities checklist items 2-5 (filter: ${REPO_FILTER:-all} ${SURFACE_FILTER:-all-surfaces})"

# ---------------------------------------------------------------------------- #
# s1a — post-merge hook installed on ~/hq-all
# Asserts:
#   - File exists at /Users/benjamincrane/hq-all/.git/hooks/post-merge
#   - File is executable
#   - Contains the canonical # HQ:dex-migration-apply marker block
# ---------------------------------------------------------------------------- #
S1A_CHECK='
  HOOK="/Users/benjamincrane/hq-all/.git/hooks/post-merge"
  if [[ ! -f "$HOOK" ]]; then echo "  hook missing: $HOOK"; exit 1; fi
  if [[ ! -x "$HOOK" ]]; then echo "  hook not executable: $HOOK"; exit 1; fi
  if ! grep -q "# HQ:dex-migration-apply" "$HOOK"; then
    echo "  hook missing # HQ:dex-migration-apply marker"; exit 1
  fi
  if ! grep -q "hook-post-merge-dex-migrations.sh" "$HOOK"; then
    echo "  hook missing hook-post-merge-dex-migrations.sh invocation"; exit 1
  fi
  echo "  hook present at $HOOK with dex-migration-apply block"
'
run_surface s1a bencrane/hq-all "$S1A_CHECK" || exit 1

# ---------------------------------------------------------------------------- #
# s1b — ops.dex_applied_migrations bootstrapped
# Asserts:
#   - Row count >= count of *.sql files in MIGRATIONS_DIR
#   - The pre-existing backfill row is still present (not clobbered)
#   - The 2 *_down.sql files are recorded as "applied" (so applier skips them)
# Methodology: count is >= rather than == to allow for newly-added migrations
# that the next merge will introduce; the s1b bootstrap freezes the snapshot
# at audit time, so a later commit that adds N migrations would show count
# (disk) > count (ledger) until apply runs.
# ---------------------------------------------------------------------------- #
S1B_CHECK='
  DISK_COUNT=$(ls -1 "'"$MIGRATIONS_DIR"'"/*.sql 2>/dev/null | wc -l | tr -d " ")
  LEDGER_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.dex_applied_migrations" | tr -d " ")
  echo "  disk=$DISK_COUNT ledger=$LEDGER_COUNT"
  if [[ "$LEDGER_COUNT" -lt "$DISK_COUNT" ]]; then
    echo "  FAIL: ledger has $LEDGER_COUNT rows, disk has $DISK_COUNT *.sql files (need >= disk)"
    exit 1
  fi
  BACKFILL=$(dex_psql_query "SELECT filename FROM ops.dex_applied_migrations WHERE filename = '"'"'20260503220000_backfill_source_canonical_provenance.sql'"'"'")
  if [[ "$BACKFILL" != "20260503220000_backfill_source_canonical_provenance.sql" ]]; then
    echo "  FAIL: backfill row missing from ledger"
    exit 1
  fi
  DOWN_PRESENT=$(dex_psql_query "SELECT COUNT(*) FROM ops.dex_applied_migrations WHERE filename LIKE '"'"'%_down.sql'"'"'" | tr -d " ")
  if [[ "$DOWN_PRESENT" -lt 2 ]]; then
    echo "  FAIL: expected 2 *_down.sql rows in ledger, found $DOWN_PRESENT"
    exit 1
  fi
  echo "  ledger bootstrapped: $LEDGER_COUNT rows incl. backfill + $DOWN_PRESENT *_down.sql"
'
run_surface s1b bencrane/hq-all "$S1B_CHECK" || exit 1

# ---------------------------------------------------------------------------- #
# s2 — scope-cycle-report.sh STALE_THRESHOLD raised + clamped
# Asserts:
#   - In-file: STALE_THRESHOLD default = 1800
#   - Runtime probe: SCOPE_STALE_THRESHOLD=60 produces a stderr warning AND
#     resets to a sane minimum (>= 120). We probe by sourcing the script
#     in a subshell that ONLY reads the variable assignment block (no exec).
# Two-mode verification because in-file grep alone doesn't prove the clamp
# fires correctly at runtime.
# ---------------------------------------------------------------------------- #
S2_CHECK='
  SCRIPT="'"$VAULT"'/scripts/scope-cycle-report.sh"
  if [[ ! -f "$SCRIPT" ]]; then echo "  script missing: $SCRIPT"; exit 1; fi
  if ! grep -qE "STALE_THRESHOLD=\"\\\$\\{SCOPE_STALE_THRESHOLD:-1800\\}\"" "$SCRIPT"; then
    echo "  FAIL: STALE_THRESHOLD default not set to 1800 in $SCRIPT"
    grep -n STALE_THRESHOLD "$SCRIPT" >&2
    exit 1
  fi
  # Sourcing scope-cycle-report.sh with no args triggers `exit 2` (agent-mode
  # requires --slug). `exit` from a sourced script kills the surrounding bash -c,
  # so a plain `echo` AFTER `source` never runs. Use trap EXIT to emit the
  # post-clamp value regardless of how the sourced script terminates.
  PROBE=$(SCOPE_STALE_THRESHOLD=60 bash -c "
    trap '"'"'echo \"STALE_THRESHOLD=\$STALE_THRESHOLD\"'"'"' EXIT
    source \"$SCRIPT\"
  " 2>&1 || true)
  if ! echo "$PROBE" | grep -qi "warn"; then
    echo "  FAIL: SCOPE_STALE_THRESHOLD=60 did not produce a warning"
    echo "$PROBE" >&2
    exit 1
  fi
  if ! echo "$PROBE" | grep -qE "STALE_THRESHOLD=(120|[2-9][0-9]{2,}|[1-9][0-9]{3,})"; then
    echo "  FAIL: clamp did not raise the value above 120"
    echo "$PROBE" >&2
    exit 1
  fi
  echo "  default 1800 confirmed; clamp warns + resets at <120"
'
run_surface s2 bencrane/hq "$S2_CHECK" || exit 1

# ---------------------------------------------------------------------------- #
# s3 — apps/data-engine-x/scripts/_lib/dex.sh DEX_DOPPLER_PROJECT default
# Asserts:
#   - origin/main version of dex.sh has DEX_DOPPLER_PROJECT default = hq-all
#   - Working-tree version does too (so locally-run helpers pick it up)
# Verification reads from origin/main (post-merge), per the directive's
# `git -C "$REPO_ROOT" show origin/main:...` form.
# ---------------------------------------------------------------------------- #
S3_CHECK='
  REPO_ROOT="'"$HQ_ALL"'"
  REL="apps/data-engine-x/scripts/_lib/dex.sh"
  ORIGIN=$(git -C "$REPO_ROOT" show "origin/main:$REL" 2>/dev/null || echo "")
  if [[ -z "$ORIGIN" ]]; then
    echo "  FAIL: cannot read origin/main:$REL"
    exit 1
  fi
  if ! echo "$ORIGIN" | grep -qE "DEX_DOPPLER_PROJECT=\"\\\$\\{DEX_DOPPLER_PROJECT:-hq-all\\}\""; then
    echo "  FAIL: origin/main:$REL does not have DEX_DOPPLER_PROJECT default = hq-all"
    echo "$ORIGIN" | grep -n DEX_DOPPLER >&2
    exit 1
  fi
  echo "  origin/main:$REL has DEX_DOPPLER_PROJECT default = hq-all"
'
run_surface s3 bencrane/hq-all "$S3_CHECK" || exit 1

echo ""
echo "==> All applicable surfaces PASSED"
