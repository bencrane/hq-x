#!/usr/bin/env bash
# Rollback harness for /scope cycle fmcsa-pipeline-remediation.
#
# Runs rollbacks in REVERSE order (s8b..s1). Per directive Operating gotchas
# and supabase/migrations/README.md §"Policy":
#
#   - s1, s5 (code: backfill script + CLAUDE.md docs) → forward-only;
#     rollback is `git revert <merge-SHA>` after merge. No in-tree action.
#   - s2a, s2b (migrations: ADD COLUMN IF NOT EXISTS) → forward-only;
#     `git revert` restores absence on next migrate run if needed.
#     IF NOT EXISTS makes re-application after revert idempotent.
#   - s3 (DEX UPDATE on ops.data_sources.health_status) → either bundle into
#     the s2a migration (so revert un-applies both) OR ship as a paired data
#     migration; rollback by `git revert <merge-SHA>` + re-deploy. NULL-on-revert
#     is the actual state (column drops; flag values vanish with it).
#   - s4 (HQ-X UPDATE on business.audience_spec_signings.freshness_status) →
#     same as s3 but on HQ-X side; bundle into s2b migration.
#   - s6 (backfill EXECUTION against prod DEX) → IMPORTANT EDGE: pre-backfill
#     state was already STALE (18 days, source_feed_date=2026-04-25). Rollback
#     returns us to stale-but-known-good, not "clean." Acceptable per directive
#     §"Iteration budget" / Operating gotchas. To rollback explicitly:
#         (a) git revert merge-SHA, (b) optionally TRUNCATE-then-INSERT from
#         an earlier R2 snapshot (e.g. snapshot=2026-04-25) — but this is
#         destructive of fresh data and the OPERATOR must authorize.
#   - s7, s7b (Railway deploys) → git revert + Railway auto-redeploys on
#     revert merge. Railway CLI v4.33.0 lacks --deployment-id per validator P7;
#     git-revert is the canonical path.
#   - s8, s8b (endpoint runtime probes) → no independent rollback; the deploy
#     rollback (s7 / s7b) takes the runtime path back.
#
# Usage:
#   ./fmcsa-pipeline-remediation-rollback.sh                              # all
#   ./fmcsa-pipeline-remediation-rollback.sh --surface s6                 # single
#   ./fmcsa-pipeline-remediation-rollback.sh --repo bencrane/hq-all
#   ./fmcsa-pipeline-remediation-rollback.sh --confirm-destructive        # required for any actual mutation
#
# Default behaviour (no --confirm-destructive) prints what WOULD be done and
# exits 0. No silent mutations.

set -euo pipefail

# shellcheck source=./_lib-shim.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

SURFACE_FILTER=""
REPO_FILTER=""
CONFIRM_DESTRUCTIVE=""
MERGE_SHA="${MERGE_SHA:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)             SURFACE_FILTER="$2"; shift 2 ;;
    --repo)                REPO_FILTER="$2";    shift 2 ;;
    --merge-sha)           MERGE_SHA="$2";      shift 2 ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE="1"; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back fmcsa-pipeline-remediation (surface=${SURFACE_FILTER:-all}, repo=${REPO_FILTER:-all}, merge-sha=${MERGE_SHA:-<unset>}, confirm-destructive=${CONFIRM_DESTRUCTIVE:-no})"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id"   ]]; then return 0; fi
  if [[ -n "$REPO_FILTER"    && "$REPO_FILTER"    != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# REVERSE order: s8b first, s1 last.

# --- s8b: HQ-X runtime probe — no independent rollback ------------------- #
rollback_surface "s8b" "bencrane/hq-all" '
  echo "    INFO: s8b runtime probe rollback is implicit via s7b (Railway revert deploy)."
'

# --- s8: DEX runtime probe — no independent rollback --------------------- #
rollback_surface "s8" "bencrane/hq-all" '
  echo "    INFO: s8 runtime probe rollback is implicit via s7 (Railway revert deploy)."
'

# --- s7b: HQ-X Railway deploy — git-revert triggers auto-rollback ------- #
rollback_surface "s7b" "bencrane/hq-all" '
  if [[ -z "$MERGE_SHA" ]]; then
    echo "    INFO: s7b rollback requires --merge-sha <sha>. Procedure:"
    echo "      git revert <merge-SHA>  # in apps/hq-x checkout"
    echo "      git push                # Railway auto-redeploys reverted SHA on hq-x service"
    return 0
  fi
  if [[ -z "$CONFIRM_DESTRUCTIVE" ]]; then
    echo "    DRY-RUN: would git-revert $MERGE_SHA (pass --confirm-destructive to act)"
    return 0
  fi
  cd "$HQ_ALL_ROOT" && git revert --no-edit "$MERGE_SHA" && git push origin main
'

# --- s7: DEX Railway deploy — git-revert triggers auto-rollback --------- #
rollback_surface "s7" "bencrane/hq-all" '
  if [[ -z "$MERGE_SHA" ]]; then
    echo "    INFO: s7 rollback requires --merge-sha <sha>. Procedure:"
    echo "      git revert <merge-SHA>  # in hq-all checkout"
    echo "      git push                # Railway auto-redeploys reverted SHA on data-engine-x service"
    return 0
  fi
  if [[ -z "$CONFIRM_DESTRUCTIVE" ]]; then
    echo "    DRY-RUN: would git-revert $MERGE_SHA (pass --confirm-destructive to act)"
    return 0
  fi
  cd "$HQ_ALL_ROOT" && git revert --no-edit "$MERGE_SHA" && git push origin main
'

# --- s6: backfill execution — pre-state was STALE (18 days) ------------- #
# Special note per directive: rollback returns to stale-but-known-good (the
# 2026-04-25 source_feed_date state). DO NOT TRUNCATE: that destroys fresh
# data without restoring older. The "rollback target" here is a deliberate
# acceptance of stale-state-as-pre-cycle-state.
rollback_surface "s6" "bencrane/hq-all" '
  echo "    INFO: s6 backfill execution rollback target is STALE state"
  echo "    (fmcsa.* tables at source_feed_date=2026-04-25 — same as pre-cycle)."
  echo "    Reverting the merge does NOT undo the data writes; the writes are"
  echo "    durable upserts in DEX Postgres. To restore explicit-stale state:"
  echo "      1. git revert $MERGE_SHA (the s1 script is forward-only)"
  echo "      2. (OPTIONAL, OPERATOR-AUTHORIZED ONLY) TRUNCATE-then-re-INSERT"
  echo "         from snapshot=2026-04-25 R2 parquets. Destructive of fresh data."
  echo "    Acceptable per directive ## Operating gotchas + Iteration budget."
'

# --- s5: DEX docs — forward-only revert --------------------------------- #
rollback_surface "s5" "bencrane/hq-all" '
  echo "    INFO: s5 rollback is git-revert <merge-SHA> per supabase/migrations/README.md §Policy."
'

# --- s4: HQ-X UPDATE on freshness_status — bundled in s2b migration ----- #
# IF NOT EXISTS on the column makes revert + re-apply idempotent.
# Reverting the merge: the column drops (next migrate re-creates it),
# UPDATE values vanish with the column.
rollback_surface "s4" "bencrane/hq-all" '
  echo "    INFO: s4 rollback is bundled into s2b migration revert."
  echo "    git revert <merge-SHA> on HQ-X migration drops freshness_status column;"
  echo "    UPDATE values vanish with the column."
'

# --- s3: DEX UPDATE on health_status — bundled in s2a migration --------- #
rollback_surface "s3" "bencrane/hq-all" '
  echo "    INFO: s3 rollback is bundled into s2a migration revert."
  echo "    git revert <merge-SHA> on DEX migration drops health_status column;"
  echo "    UPDATE values vanish with the column."
'

# --- s2b: HQ-X migration — forward-only revert -------------------------- #
rollback_surface "s2b" "bencrane/hq-all" '
  echo "    INFO: s2b rollback is git-revert <merge-SHA> on apps/hq-x/migrations/."
  echo "    The freshness_status column is additive; ADD COLUMN IF NOT EXISTS"
  echo "    means re-applying after revert is idempotent. If post-revert the"
  echo "    column must be physically removed, manually run:"
  echo "      cd apps/hq-x && doppler run --project hq-all --config prd -- bash -c '\''psql \"\$HQX_DB_URL_DIRECT\" -c \"ALTER TABLE business.audience_spec_signings DROP COLUMN IF EXISTS freshness_status;\"'\''"
'

# --- s2a: DEX migration — forward-only revert --------------------------- #
rollback_surface "s2a" "bencrane/hq-all" '
  echo "    INFO: s2a rollback is git-revert <merge-SHA> on apps/data-engine-x/supabase/migrations/."
  echo "    The health_status column is additive; ADD COLUMN IF NOT EXISTS"
  echo "    means re-applying after revert is idempotent. If post-revert the"
  echo "    column must be physically removed, manually run:"
  echo "      dex_psql_ddl \"ALTER TABLE ops.data_sources DROP COLUMN IF EXISTS health_status;\""
'

# --- s1: NEW backfill script — forward-only revert ---------------------- #
rollback_surface "s1" "bencrane/hq-all" '
  echo "    INFO: s1 rollback is git-revert <merge-SHA> per supabase/migrations/README.md §Policy."
  echo "    The s1 script is a one-shot (executed in s6) — removing it from the"
  echo "    tree has no runtime effect once s6 has run."
'

echo ""
echo "All requested rollbacks dispatched."
