#!/usr/bin/env bash
# Rollback harness for the clinicaltrials-device-studies substrate.
#
# Original cycle: clinicaltrials-device-studies-ingest (2026-05-20, PR #584).
# Updated by:     clinicaltrials-device-studies-aact-refresh (2026-05-20) —
#   directive reference refreshed; rollback mechanics are unchanged (the
#   AACT-refresh PR is a single squash-merge, reverted by its merge SHA).
#
# Per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-20-clinicaltrials-device-studies-aact-refresh-fix.md
#
# CANONICAL IN-REPO PATH:
#   apps/data-engine-x/scripts/migration-checks/clinicaltrials-device-studies.rollback.sh
#
# Pattern: mirrors `ca-cal-eprocure-archived-ingest-rollback.sh` (sub-A, PR #551).
#
# Rollback order — REVERSE of verify order:
#   Phase 5 (Modal):  s1
#   Phase 4 (Lance):  e1
#   Phase 3 (R2):     r1
#   Phase 2 (Code):   c6, c4, c2, c1
#   Phase 1 (Migr):   m2, m1
#
# Forward-only DB policy (apps/data-engine-x/supabase/migrations/README.md "Policy"):
# m1/m2 do NOT have paired _down.sql files. Their rollback is `git revert <merge-SHA>`
# after merge; the `CREATE TABLE IF NOT EXISTS` / `INSERT ... ON CONFLICT DO NOTHING`
# shape makes re-application idempotent. Pre-merge rollback for code surfaces is
# implicit: don't merge the PR.
#
# Usage:
#   ./clinicaltrials-device-studies.rollback.sh                       # all surfaces
#   ./clinicaltrials-device-studies.rollback.sh --surface s1          # one surface
#   ./clinicaltrials-device-studies.rollback.sh --repo bencrane/hq-all # repo filter
#   MERGE_SHA=<sha> ./clinicaltrials-device-studies.rollback.sh       # required for post-merge code revert

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

echo "==> Rolling back clinicaltrials-device-studies-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"
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
R2_PREFIX="clinicaltrials-gov/device-studies"
LANCE_URI="s3://${R2_BUCKET}/polaris-warehouse/clinicaltrials/device_studies_lance"
MODAL_APP_NAME="data-engine-x-clinicaltrials-device-studies"

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
# Phase 4 — Delete Lance dataset at R2
# ====================================================================== #

# ── e1: delete the Lance dataset ──────────────────────────────────────── #
rollback_surface "e1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm '"$LANCE_URI"' --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (lance dataset already absent)\"
  "
'

# ====================================================================== #
# Phase 3 — Delete R2 raw snapshot partitions
# ====================================================================== #

# ── r1: delete R2 raw snapshot partitions ─────────────────────────────── #
rollback_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET/$R2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (r2 prefix already empty)\"
  "
'

# ====================================================================== #
# Phase 2 — Code reverts (post-merge only)
# ====================================================================== #
# Single PR — all code surfaces share the same merge SHA. One git revert
# rolls back all of c1/c2/c4/c6 (and m1/m2 files).

# ── c6: revert LanceView entry ────────────────────────────────────────── #
rollback_surface "c6" "bencrane/hq-all" "_git_revert_if_merged '$MERGE_SHA'"

# ── c4: revert Lance emit script ──────────────────────────────────────── #
rollback_surface "c4" "bencrane/hq-all" '
  echo "  (c4 rolled back by the c6 git revert above — same merge SHA)"
'

# ── c2: revert Modal app file ─────────────────────────────────────────── #
rollback_surface "c2" "bencrane/hq-all" '
  echo "  (c2 rolled back by the c6 git revert above — same merge SHA)"
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
  echo "  (m2 rolled back by the c6 git revert above — same merge SHA. Table ops.clinicaltrials_device_studies_ingest_runs remains in prod; remove manually if required: DROP TABLE ops.clinicaltrials_device_studies_ingest_runs CASCADE. NOTE: the m2 migration also recreated ops.data_source_catalog_status — a git revert removes the file but the live view keeps the clinicaltrials branch; the branch is harmless when the catalog row is removed.)"
'

# ── m1: revert data_source_catalog INSERT ─────────────────────────────── #
rollback_surface "m1" "bencrane/hq-all" '
  echo "  (m1 rolled back by the c6 git revert above — same merge SHA. Catalog row remains in prod; remove manually if required: UPDATE ops.data_source_catalog SET is_active=FALSE WHERE source_slug='"'"'clinicaltrials_device_studies'"'"')"
'

# ====================================================================== #
echo ""
echo "==> ROLLBACK SUMMARY: $OK_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
