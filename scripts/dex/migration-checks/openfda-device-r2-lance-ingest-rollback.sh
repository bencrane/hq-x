#!/usr/bin/env bash
# Rollback harness for /scope cycle `openfda-device-r2-lance-ingest`.
#
# Authored by the Stage 3.A migration auditor per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-20-openfda-device-r2-lance-ingest.md.
# Pattern: mirrors `caltrans-ccop-active-ingest-rollback.sh` (PR #552).
#
# All 7 surfaces land in ONE squash-merged PR (bencrane/hq-all). The
# migration + code surfaces (s1-s6) are reverted forward-only via a single
# `git revert <merge-SHA>` (supabase/migrations/README.md §"Policy" — no
# `_down.sql`). The s7 deploy is rolled back with `modal app stop` (brand-new
# app, no prior deployment to redeploy).
#
# Rollback order — REVERSE of verify surface order:
#   Phase 5 (Modal deploy):  s7  → modal app stop
#   Phase 4 (Lance emit):    e1  → aws s3 rm the 3 Lance datasets
#   Phase 3 (R2 backfill):   r1  → aws s3 rm the 3 R2 raw prefixes
#   Phase 2 (Code):          s6, s5, s4, s3  → single git revert <merge-SHA>
#   Phase 1 (Migrations):    s2, s1          → same git revert (forward-only DB policy)
#
# DB objects (ops.openfda_device_ingest_runs table, 3 ops.data_source_catalog
# rows) are NOT auto-dropped by the git revert — the revert removes the
# migration FILES, not their applied effect. The s2/s1 stanzas below print the
# exact manual cleanup statements if a full teardown is required.
#
# The legacy entities.openfda_device_* Postgres tables are OUT OF SCOPE and are
# NEVER touched by this rollback (directive `## Out of scope`).
#
# Usage:
#   ./openfda-device-r2-lance-ingest-rollback.sh                       # all surfaces
#   ./openfda-device-r2-lance-ingest-rollback.sh --surface s7          # one surface
#   ./openfda-device-r2-lance-ingest-rollback.sh --repo hq-all         # repo filter
#   MERGE_SHA=<sha> ./openfda-device-r2-lance-ingest-rollback.sh       # do the git revert
#   ./openfda-device-r2-lance-ingest-rollback.sh --merge-sha <sha>     # same, via flag

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

# Note: this rollback harness needs only $HQ_ALL_ROOT (for the git revert
# checkout) — no $APP_DIR. The verify harness keeps APP_DIR for its file checks.

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

echo "==> Rolling back openfda-device-r2-lance-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"
if [[ -n "$MERGE_SHA" ]]; then
  echo "==> MERGE_SHA=$MERGE_SHA"
else
  echo "==> No MERGE_SHA — code/migration reverts SKIPPED (pre-merge implicit-rollback path)"
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
R2_PREFIX_510K="openfda/device/510k"
R2_PREFIX_PMA="openfda/device/pma"
R2_PREFIX_CLASSIFICATION="openfda/device/classification"
LANCE_URI_510K="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_510k_lance"
LANCE_URI_PMA="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_pma_lance"
LANCE_URI_CLASSIFICATION="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_classification_lance"
MODAL_APP_NAME="data-engine-x-openfda-device"

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

# ── s7: stop the Modal app ───────────────────────────────────────────── #
rollback_surface "s7" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"' 2>/dev/null || echo \"  (modal app already stopped or not deployed)\"
  "
'

# ── e1: delete the 3 Lance datasets ──────────────────────────────────── #
rollback_surface "e1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    for URI in '"$LANCE_URI_510K"' '"$LANCE_URI_PMA"' '"$LANCE_URI_CLASSIFICATION"'; do
      AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
        aws s3 rm \$URI --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
        echo \"  (lance dataset already absent: \$URI)\"
    done
  "
'

# ── r1: delete the 3 R2 raw prefixes ─────────────────────────────────── #
rollback_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    for PREFIX in '"$R2_PREFIX_510K"' '"$R2_PREFIX_PMA"' '"$R2_PREFIX_CLASSIFICATION"'; do
      AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
        aws s3 rm s3://'"$R2_BUCKET"'/\$PREFIX/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null || \
        echo \"  (r2 prefix already empty: \$PREFIX)\"
    done
  "
'

# ── s6, s5, s4, s3: single-merge-SHA git revert ──────────────────────── #
rollback_surface "s6" "bencrane/hq-all" "_git_revert_if_merged '$MERGE_SHA'"
rollback_surface "s5" "bencrane/hq-all" 'echo "  (s5 rolled back by the s6 git revert above — same merge SHA)"'
rollback_surface "s4" "bencrane/hq-all" 'echo "  (s4 rolled back by the s6 git revert above — same merge SHA)"'
rollback_surface "s3" "bencrane/hq-all" 'echo "  (s3 rolled back by the s6 git revert above — same merge SHA)"'

# ── s2, s1: same git revert (forward-only DB policy) ─────────────────── #
rollback_surface "s2" "bencrane/hq-all" '
  echo "  (s2 rolled back by the s6 git revert above. Table ops.openfda_device_ingest_runs remains in prod; remove manually if required: DROP TABLE ops.openfda_device_ingest_runs CASCADE)"
'
rollback_surface "s1" "bencrane/hq-all" '
  echo "  (s1 rolled back by the s6 git revert above. 3 catalog rows remain in prod; remove manually if required: UPDATE ops.data_source_catalog SET is_active=FALSE WHERE source_slug LIKE '"'"'openfda_device_%'"'"'. The data_source_catalog_status view keeps the 3 branches until a later migration recreates it without them.)"
'

echo ""
echo "==> ROLLBACK SUMMARY: $OK_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
