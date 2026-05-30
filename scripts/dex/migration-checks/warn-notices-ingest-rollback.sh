#!/usr/bin/env bash
# Rollback harness for cycle `warn-notices-bln-ingest` (2026-05-20).
# Pattern: mirrors `caltrans-ccop-active-ingest-rollback.sh`.
#
# Rollback order — REVERSE of verify order:
#   Phase 5 (Modal):  s1
#   Phase 4 (Lance):  e1
#   Phase 3 (R2):     r1
#   Phase 2 (Code):   c6, c4, c2, c1
#   Phase 1 (Migr):   m2, m1
#
# Note: this cycle UPSERTed the pre-existing warn_notices catalog row and
# created the NEW ops.warn_notices_r2_ingest_runs ledger. A git revert restores
# the code; the DB objects are forward-only and remain. The dormant pre-cycle
# pipeline (entities.source_warn_notices, old ops.warn_notices_ingest_runs) was
# left untouched by this cycle and needs no rollback.

set -euo pipefail

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

echo "==> Rolling back warn-notices-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"
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

R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="warn/notices"
LANCE_URI="s3://${R2_BUCKET}/polaris-warehouse/warn/notices_lance"
MODAL_APP_NAME="data-engine-x-warn-notices"

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

# ── s1 ───────────────────────────────────────────────────────────────── #
rollback_surface "s1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"' 2>/dev/null || echo \"  (modal app already stopped or not deployed)\"
  "
'

# ── e1: delete Lance dataset ─────────────────────────────────────────── #
rollback_surface "e1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm '"$LANCE_URI"' --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (lance dataset already absent)\"
  "
'

# ── r1: delete R2 raw snapshots ──────────────────────────────────────── #
rollback_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET/$R2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
      echo \"  (r2 prefix already empty)\"
  "
'

# ── c6, c4, c2, c1: single-merge-SHA git revert ──────────────────────── #
rollback_surface "c6" "bencrane/hq-all" "_git_revert_if_merged '$MERGE_SHA'"
rollback_surface "c4" "bencrane/hq-all" 'echo "  (c4 rolled back by the c6 git revert above — same merge SHA)"'
rollback_surface "c2" "bencrane/hq-all" 'echo "  (c2 rolled back by the c6 git revert above — same merge SHA)"'
rollback_surface "c1" "bencrane/hq-all" 'echo "  (c1 rolled back by the c6 git revert above — same merge SHA)"'

# ── m2, m1: forward-only DB policy — git revert restores code only ───── #
rollback_surface "m2" "bencrane/hq-all" '
  echo "  (m2 rolled back by the c6 git revert above. Table ops.warn_notices_r2_ingest_runs remains in prod (forward-only); the status view keeps the warn_notices branch pointed at it. Remove manually if required: DROP TABLE ops.warn_notices_r2_ingest_runs CASCADE)"
'
rollback_surface "m1" "bencrane/hq-all" '
  echo "  (m1 rolled back by the c6 git revert above. The warn_notices catalog row was UPSERTed from a prior discovery-stage config; git revert does NOT restore the prior row values. Restore manually if required, or leave — the row is orphan-safe.)"
'

echo ""
echo "==> ROLLBACK SUMMARY: $OK_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
