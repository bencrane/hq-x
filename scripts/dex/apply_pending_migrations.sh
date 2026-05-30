#!/usr/bin/env bash
# Apply any pending data-engine-x migrations against the live DB (forward-only,
# `IF NOT EXISTS` per apps/data-engine-x/supabase/migrations/README.md §"Policy").
#
# Tracks applied migrations in ops.dex_applied_migrations (created on first run).
# Diffs the migrations dir against that table and applies anything new in
# lexicographic (timestamp-prefix) order. Each migration runs in its own
# transaction; first failure halts and surfaces.
#
# Invoked by:
#   1. ~/Desktop/hq/scripts/hook-post-merge-dex-migrations.sh (vault post-merge wrapper)
#      after a PR merges into bencrane/hq-all main and the operator pulls.
#   2. Manually: cd apps/data-engine-x && ./scripts/apply_pending_migrations.sh [--dry-run]
#
# Doppler context comes from the per-directory doppler.yaml; ensure
# `doppler setup --no-interactive` has been run in apps/data-engine-x/.

set -euo pipefail

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Defense in depth against env-var bleed when invoked by a hook firing
# from a worktree-context (see hook-post-merge-dex-migrations.sh comment).
# These vars also get unset there before this script runs; setting them
# unset here makes manual invocations equally robust.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR

# Resolve repo root from this script's location rather than `git rev-parse`,
# which is sensitive to env vars and cwd. apps/data-engine-x/scripts/ is
# three levels below the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -z "$REPO_ROOT" ]] || [[ ! -d "$REPO_ROOT/.git" && ! -f "$REPO_ROOT/.git" ]]; then
  echo "FAIL: could not resolve repo root from script location ($SCRIPT_DIR)" >&2
  exit 1
fi

MIGRATIONS_DIR="$REPO_ROOT/apps/data-engine-x/supabase/migrations"
LIB="$REPO_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
  echo "FAIL: migrations dir missing: $MIGRATIONS_DIR" >&2
  exit 1
fi
if [[ ! -f "$LIB" ]]; then
  echo "FAIL: helper library missing: $LIB" >&2
  exit 1
fi

# shellcheck source=_lib/dex.sh
source "$LIB"

# Ledger table — idempotent.
dex_psql_ddl "
  CREATE SCHEMA IF NOT EXISTS ops;
  CREATE TABLE IF NOT EXISTS ops.dex_applied_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    sha256 text NOT NULL
  );
" >/dev/null

# Cache the applied set as a sorted, newline-separated list for grep.
APPLIED=$(dex_psql_query "SELECT filename FROM ops.dex_applied_migrations ORDER BY filename")

count_applied=0
count_skipped=0
count_pending=0
count_down_skipped=0

for migration in "$MIGRATIONS_DIR"/*.sql; do
  [[ -f "$migration" ]] || continue
  name=$(basename "$migration")

  # Skip *_down.sql files unconditionally (forward-only policy per
  # supabase/migrations/README.md "Policy"). Even if the ledger has a row
  # for a *_down.sql, we never re-apply one.
  if [[ "$name" == *_down.sql ]]; then
    echo "==> SKIP DOWN: $name (forward-only policy per supabase/migrations/README.md \"Policy\")"
    count_down_skipped=$((count_down_skipped + 1))
    continue
  fi

  # Skip README and other non-migration .sql artifacts (none today, but defensive).
  if [[ "$name" == "README.md" ]]; then continue; fi

  if grep -Fxq "$name" <<<"$APPLIED"; then
    count_skipped=$((count_skipped + 1))
    continue
  fi

  count_pending=$((count_pending + 1))

  if (( DRY_RUN == 1 )); then
    echo "WOULD APPLY: $name"
    continue
  fi

  echo "==> Applying: $name"
  if dex_psql_ddl_file "$migration"; then
    sha=$(shasum -a 256 "$migration" | awk '{print $1}')
    dex_psql_ddl "INSERT INTO ops.dex_applied_migrations (filename, sha256) VALUES ('$name', '$sha') ON CONFLICT (filename) DO NOTHING" >/dev/null
    count_applied=$((count_applied + 1))
    echo "    OK"
  else
    echo "    FAIL — halting; manual intervention required" >&2
    exit 1
  fi
done

# Detect orphans: rows in ops.dex_applied_migrations whose filename no
# longer exists on disk in MIGRATIONS_DIR (e.g. removed by `git revert`).
# Warn but never fail — the operator decides whether to clean up the row.
count_orphan_warned=0
while IFS= read -r ledger_name; do
  [[ -z "$ledger_name" ]] && continue
  if [[ ! -f "$MIGRATIONS_DIR/$ledger_name" ]]; then
    echo "==> WARN ORPHAN: $ledger_name (in ledger but not on disk)" >&2
    count_orphan_warned=$((count_orphan_warned + 1))
  fi
done <<<"$APPLIED"

echo ""
if (( DRY_RUN == 1 )); then
  echo "Dry-run: $count_pending pending, $count_skipped already applied, $count_down_skipped down-skipped, $count_orphan_warned orphan-warned"
else
  echo "Applied: $count_applied  Skipped: $count_skipped  Down-skipped: $count_down_skipped  Orphan-warned: $count_orphan_warned"
fi
