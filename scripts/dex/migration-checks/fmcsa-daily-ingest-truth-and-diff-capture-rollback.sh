#!/usr/bin/env bash
# Rollback harness for /scope cycle fmcsa-daily-ingest-truth-and-diff-capture.
#
# Authored by Stage 3.A audit subagent (2026-05-12 UTC) per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-12-fmcsa-daily-ingest-truth-and-diff-capture.md.
#
# Rollback runs in REVERSE order of forward apply (s9 → s8 → s7 → s6 → s5
# → s3 → s2 → s1). The cycle is mostly forward-only; rollback for code
# changes is `git revert <merge-SHA>`. The destructive surface is s2 (R2
# lifecycle policy) — rollback restores prior config from the captured JSON
# at /tmp/dex-raw-landing-zone-lifecycle-prior.json (must exist; see
# verify-script s0 pre-flight).
#
# Usage:
#   MERGE_SHA=<sha> ./fmcsa-daily-ingest-truth-and-diff-capture-rollback.sh
#
# NOTE: This script is informational + lifecycle-restoration. For code/migration
# rollback, the operator runs `git revert <MERGE_SHA> && git push` — Railway
# auto-deploy + manual `modal deploy` then restore prior prod state.

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

if [[ -z "${MERGE_SHA:-}" ]]; then
  echo "FAIL: MERGE_SHA env must be set (the merge SHA to revert)" >&2
  exit 2
fi

echo "==> Rollback fmcsa-daily-ingest-truth-and-diff-capture (MERGE_SHA=$MERGE_SHA)"
echo "    Order: s9 → s8 → s7 → s6 → s5 → s3 → s2 → s1"
echo ""

# ── s9 rollback ──────────────────────────────────────────────────────── #
# No rollback at endpoint; rollback is via s8.
echo "-- s9: no rollback (endpoint probe; rolled back via s8)"

# ── s8 rollback ──────────────────────────────────────────────────────── #
# Railway auto-deploys the revert. Modal redeploy of prior `material_change_
# detection_app.py` is manual + occurs after `git revert` lands the prior
# code on main.
cat <<EOF
-- s8: MANUAL — operator/executor must:
       1. git -C "$HQ_ALL_ROOT" revert --no-edit "$MERGE_SHA"
       2. git -C "$HQ_ALL_ROOT" push origin main
       3. Wait for Railway auto-deploy SUCCESS:
            doppler run --project hq-all --config prd -- bash -c \\
              "cd $HQ_ALL_ROOT/apps/data-engine-x && railway status --service data-engine-x --json | jq -r .latestDeployment.status"
       4. Modal redeploy of REVERTED code:
            cd $HQ_ALL_ROOT/apps/data-engine-x && \\
              doppler run --project hq-all --config prd -- bash -c \\
                "modal deploy modal/material_change_detection_app.py"
EOF

# ── s7 rollback ──────────────────────────────────────────────────────── #
# CLAUDE.md change reverts with the merge.
echo "-- s7: rolls back via git revert (CLAUDE.md change in same merge)"

# ── s6 rollback ──────────────────────────────────────────────────────── #
# Script file removed via git revert.
echo "-- s6: rolls back via git revert (verify_daily_ingest.py removed)"

# ── s5 rollback ──────────────────────────────────────────────────────── #
# Forward-only migration. Per CLAUDE.md migration policy, rollback for
# additive changes = git revert. The dropped-index path is OPTIONAL; only
# invoke if operator wants the index gone in prod before the revert lands.
echo ""
echo "-- s5: forward-only migration. Index drop is OPTIONAL pre-revert:"
echo "       (Run only if operator wants index removed BEFORE git revert lands.)"
read -p "       Drop ops.idx_material_events_run_attribute now? [y/N] " yn
if [[ "$yn" == "y" || "$yn" == "Y" ]]; then
  dex_psql_ddl 'DROP INDEX CONCURRENTLY IF EXISTS ops.idx_material_events_run_attribute'
  echo "       Index dropped."
else
  echo "       SKIPPED — index remains; git revert removes the migration file from the forward path."
fi

# ── s3 rollback ──────────────────────────────────────────────────────── #
# Code change reverts with the merge. After revert + Modal redeploy, the
# resolver glob path returns to 'fmcsa-carrier-essentials/...' which matches
# no R2 objects → resolver returns None → detector silently skips FMCSA
# (status-quo-ante).
echo "-- s3: rolls back via git revert (material_change_detector.py path returns to broken state)"

# ── s2 rollback — RESTORE PRIOR R2 LIFECYCLE CONFIG ───────────────────── #
# This is the destructive surface. Prior config MUST exist at
# /tmp/dex-raw-landing-zone-lifecycle-prior.json (the pre-flight captured it).
PRIOR_JSON=/tmp/dex-raw-landing-zone-lifecycle-prior.json
echo ""
echo "-- s2: restore prior R2 lifecycle config"
if [[ ! -f "$PRIOR_JSON" ]]; then
  echo "       FAIL: $PRIOR_JSON not found." >&2
  echo "       Search the audit trail for the captured config (it was a pre-flight gate)." >&2
  echo "       Without prior config, MANUAL recovery is required:" >&2
  echo "         1. Inspect docs/r2-retention-policy.md (commit-history) for the fenced code block capturing prior config." >&2
  echo "         2. Save that JSON to $PRIOR_JSON." >&2
  echo "         3. Re-run this script." >&2
  exit 1
fi
echo "       Restoring from $PRIOR_JSON..."
_dex_doppler "aws s3api put-bucket-lifecycle-configuration --bucket dex-raw-landing-zone --endpoint-url \"\$R2_ENDPOINT\" --lifecycle-configuration file://$PRIOR_JSON"
echo "       Restored."

# ── s1 rollback ──────────────────────────────────────────────────────── #
# Docs file removed via git revert.
echo "-- s1: rolls back via git revert (docs/fmcsa-daily-pipeline.md removed)"

echo ""
echo "==> Rollback orchestration complete."
echo "    Next manual steps (if not done above):"
echo "      git -C $HQ_ALL_ROOT revert --no-edit $MERGE_SHA && git -C $HQ_ALL_ROOT push origin main"
echo "      cd $HQ_ALL_ROOT/apps/data-engine-x && \\"
echo "        doppler run --project hq-all --config prd -- bash -c \\"
echo "          'modal deploy modal/material_change_detection_app.py'"
exit 0
