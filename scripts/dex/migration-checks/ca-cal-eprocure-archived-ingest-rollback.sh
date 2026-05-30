#!/usr/bin/env bash
# Rollback harness for cycle `ca-cal-eprocure-archived-ingest` (2026-05-19).
#
# Authored 2026-05-19 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-19-ca-cal-eprocure-archived-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/ca-cal-eprocure-archived-ingest-rollback.sh
#
# Pattern: mirrors `2026-05-18-sec-dera-form-d-ingest-rollback.sh`.
#
# Rollback order — REVERSE of verify order:
#   Phase 5 (Modal):  s1
#   Phase 4 (Lance):  e2, e1
#   Phase 3 (R2):     r2, r1
#   Phase 2 (Code):   c6, c5, c3, c1
#   Phase 1 (Migr):   m2, m1
#
# Forward-only DB policy (apps/data-engine-x/supabase/migrations/README.md §"Policy"):
# m1/m2 do NOT have paired _down.sql files. Their rollback is `git revert <merge-SHA>`
# after merge; the `CREATE TABLE IF NOT EXISTS` / `INSERT … ON CONFLICT DO NOTHING`
# shape makes re-application idempotent. Pre-merge rollback for code surfaces is
# implicit: don't merge the PR.
#
# Usage:
#   ./ca-cal-eprocure-archived-ingest-rollback.sh                       # all surfaces
#   ./ca-cal-eprocure-archived-ingest-rollback.sh --surface s1          # one surface
#   ./ca-cal-eprocure-archived-ingest-rollback.sh --repo bencrane/hq-all # repo filter
#   MERGE_SHA=<sha> ./ca-cal-eprocure-archived-ingest-rollback.sh       # required for post-merge code revert

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
else
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

APP_DIR="$HQ_ALL_ROOT/apps/data-engine-x"

# --- CLI parsing --------------------------------------------------------- #
REPO_FILTER=""
SURFACE_FILTER=""
MERGE_SHA="${MERGE_SHA:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)       REPO_FILTER="$2"; shift 2 ;;
    --surface)    SURFACE_FILTER="$2"; shift 2 ;;
    --merge-sha)  MERGE_SHA="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back ca-cal-eprocure-archived-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"
if [[ -n "$MERGE_SHA" ]]; then
  echo "==> MERGE_SHA=$MERGE_SHA (post-merge code revert path)"
else
  echo "==> No MERGE_SHA set — code-surface reverts are SKIPPED (pre-merge implicit-rollback path)"
fi

FAIL_COUNT=0
OK_COUNT=0
SKIP_COUNT=0

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
    OK_COUNT=$((OK_COUNT+1))
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
    return 1
  fi
}

# --- pinned constants (must match verify harness) ---------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PO_PREFIX="cal-eprocure-archived/po"
R2_NCB_PREFIX="cal-eprocure-archived/ncb"
LANCE_PO_URI="s3://${R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_po_lance"
LANCE_NCB_URI="s3://${R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_ncb_lance"
MODAL_APP_NAME="data-engine-x-cal-eprocure-archived"

# Code reverts use git revert of the merge SHA. Pre-merge: skip (implicit
# rollback = don't merge). Post-merge: revert via the merge SHA.
_git_revert_if_merged() {
  local sha="$1"
  if [[ -z "$sha" ]]; then
    echo "  (pre-merge path: skip; rollback implicit by not merging)"
    return 0
  fi
  cd "$HQ_ALL_ROOT" || return 1
  git fetch origin main &&
  git checkout main &&
  git pull --ff-only &&
  git revert --no-edit "$sha" &&
  git push origin main
}

# ====================================================================== #
# Phase 5 (FIRST in rollback) — Modal app stop
# ====================================================================== #

# ── s1: stop the Modal app ─────────────────────────────────────────────── #
rollback_surface "s1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"' 2>/dev/null || echo \"  (modal app already stopped or not deployed)\"
  "
'

# ====================================================================== #
# Phase 4 — Delete Lance datasets at R2 (executor rollback)
# ====================================================================== #

# ── e2: delete NCB Lance dataset ──────────────────────────────────────── #
rollback_surface "e2" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm '"$LANCE_NCB_URI"' --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (lance ncb dataset already absent)\"
  "
'

# ── e1: delete PO Lance dataset ───────────────────────────────────────── #
rollback_surface "e1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm '"$LANCE_PO_URI"' --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (lance po dataset already absent)\"
  "
'

# ====================================================================== #
# Phase 3 — Delete R2 raw snapshot partitions
# ====================================================================== #

# ── r2: delete NCB R2 partitions ──────────────────────────────────────── #
rollback_surface "r2" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET/$R2_NCB_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (r2 ncb prefix already empty)\"
  "
'

# ── r1: delete PO R2 partitions ───────────────────────────────────────── #
rollback_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET/$R2_PO_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (r2 po prefix already empty)\"
  "
'

# ====================================================================== #
# Phase 2 — Code reverts (post-merge only)
# ====================================================================== #

# ── c6: revert LanceView entries ──────────────────────────────────────── #
rollback_surface "c6" "bencrane/hq-all" "_git_revert_if_merged '$MERGE_SHA'"

# ── c5: revert Lance emit script ──────────────────────────────────────── #
# Code surfaces share the same merge SHA — single PR. The revert command above
# handles ALL code surfaces in one git revert (post-merge). For per-surface
# granularity, the executor would need to split the PR — out of scope this cycle.
rollback_surface "c5" "bencrane/hq-all" '
  echo "  (c5 rolled back by the c6 git revert above — same merge SHA)"
'

# ── c3: revert Modal app file ─────────────────────────────────────────── #
rollback_surface "c3" "bencrane/hq-all" '
  echo "  (c3 rolled back by the c6 git revert above — same merge SHA)"
'

# ── c1: revert R2 ingest script ───────────────────────────────────────── #
rollback_surface "c1" "bencrane/hq-all" '
  echo "  (c1 rolled back by the c6 git revert above — same merge SHA)"
'

# ====================================================================== #
# Phase 1 — Migration reverts (post-merge only)
# ====================================================================== #
# Forward-only: revert removes the migration FILE; tables/rows remain in prod.
# Re-application via apply_pending_migrations.sh is idempotent (IF NOT EXISTS).

# ── m2: revert audit-ledger migration ─────────────────────────────────── #
rollback_surface "m2" "bencrane/hq-all" '
  echo "  (m2 rolled back by the c6 git revert above — same merge SHA. Table ops.cal_eprocure_ingest_runs remains in prod; remove manually if required: DROP TABLE ops.cal_eprocure_ingest_runs CASCADE)"
'

# ── m1: revert data_source_catalog INSERTs ────────────────────────────── #
rollback_surface "m1" "bencrane/hq-all" '
  echo "  (m1 rolled back by the c6 git revert above — same merge SHA. Catalog rows remain in prod; remove manually if required: UPDATE ops.data_source_catalog SET is_active=FALSE WHERE source_slug IN ('"'"'cal_eprocure_archived_po'"'"','"'"'cal_eprocure_archived_ncb'"'"'))"
'

# ====================================================================== #
echo ""
echo "==> ROLLBACK SUMMARY: $OK_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
