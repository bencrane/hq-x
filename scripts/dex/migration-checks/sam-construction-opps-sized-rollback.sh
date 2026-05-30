#!/usr/bin/env bash
# Rollback harness for cycle `sam-construction-opps-sized` (2026-05-19).
#
# COHORT-EMIT framing (NOT an identity bridge — per L28):
#   This cycle ships a Pattern A enriched-cohort Lance dataset. It does NOT
#   create an ops.bridges row, ops.match_method* rows, or any ledger table.
#   The ONLY DB write is the m1 ops.data_source_catalog row +
#   ops.data_source_catalog_status view recreation.
#   Rollback removes ONLY: the Modal app, the Lance dataset at R2, the four
#   code/migration files (one git revert of the merge SHA), and — optionally,
#   manually — flips the catalog row is_active=FALSE.
#   There are NO shared identity rows at risk; nothing to preserve.
#
# Forward-only migrations per supabase/migrations/README.md §"Policy".
# Per-surface rollback in reverse-order:
#   s1 → e2 → e1 → r1 → c6 → c2 → c1 → m1
# s1 is a two-step (modal app stop, then git revert).
# c1/c2/c6/m1 are rolled back by a single git revert of the merge commit
# (additive ON-CONFLICT-DO-NOTHING / CREATE-OR-REPLACE migration; revert is
# forward-only safe).

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
    --repo)      REPO_FILTER="$2"; shift 2 ;;
    --surface)   SURFACE_FILTER="$2"; shift 2 ;;
    --merge-sha) MERGE_SHA="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back sam-construction-opps-sized (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"
if [[ -n "$MERGE_SHA" ]]; then
  echo "==> MERGE_SHA=$MERGE_SHA"
else
  echo "==> No MERGE_SHA — code reverts SKIPPED (pre-merge implicit-rollback path)"
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

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="polaris-warehouse/bridges/construction_opps_sized_lance"
LANCE_URI="s3://${R2_BUCKET}/${R2_PREFIX}"
MODAL_APP_NAME="data-engine-x-sam-construction-opps-sized"

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
# Reverse-order rollback: s1 → e2 → e1 → r1 → c6 → c2 → c1 → m1
# ====================================================================== #

# ── s1: Modal app stop (disable cron + remove deployed app) ──────────── #
rollback_surface "s1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    modal app stop '"$MODAL_APP_NAME"' 2>/dev/null || echo \"  (modal app already absent)\"
  "
'

# ── e2: DB cleanup — NOTHING to delete (cohort emit, no ledger / bridge) ─ #
# Per L28 this cohort emit writes NO ops.bridges, NO ops.match_method* rows,
# NO ledger table. The only DB artifact is the m1 catalog row (handled by the
# m1 step below). No deletes here.
rollback_surface "e2" "bencrane/hq-all" '
  echo "  (no DB cleanup — cohort emit creates no ops.bridges / ledger rows per L28; m1 catalog row handled below)"
'

# ── e1: Lance dataset removal ────────────────────────────────────────── #
rollback_surface "e1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm '"$LANCE_URI"' --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (lance dataset already absent)\"
  "
'

# ── r1: R2 raw prefix cleanup (same prefix as e1; reuse for symmetry) ─ #
rollback_surface "r1" "bencrane/hq-all" '
  echo "  (r1 == e1 for this dataset — R2 prefix already cleaned by e1 step above)"
'

# ── c6: revert lance_views.py entry (git revert) ─────────────────────── #
rollback_surface "c6" "bencrane/hq-all" "_git_revert_if_merged '$MERGE_SHA'"

# ── c2: Modal app source file (rolled back by c6 git revert) ─────────── #
rollback_surface "c2" "bencrane/hq-all" '
  echo "  (c2 rolled back by the c6 git revert above — same merge SHA)"
'

# ── c1: emit script file (rolled back by c6 git revert) ──────────────── #
rollback_surface "c1" "bencrane/hq-all" '
  echo "  (c1 rolled back by the c6 git revert above — same merge SHA)"
'

# ── m1: migration file (rolled back by c6 git revert; view recreation
#       reverts to prior body; catalog row persists in DB) ────────────── #
rollback_surface "m1" "bencrane/hq-all" '
  echo "  (m1 file removed by git revert above. Catalog row remains in prod (append-only convention); to hide from dashboard run manually:" &&
  echo "     UPDATE ops.data_source_catalog SET is_active=FALSE WHERE source_slug='\''sam_construction_opps_sized'\'';" &&
  echo "   The view recreation in m1 reverts on revert — the new sam_construction_opps_sized branch disappears from data_source_catalog_status.)"
'

echo ""
echo "==> ROLLBACK SUMMARY: $OK_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
