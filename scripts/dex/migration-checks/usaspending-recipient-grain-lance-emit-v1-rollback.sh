#!/usr/bin/env bash
# Rollback harness for /scope cycle `usaspending-recipient-grain-lance-emit-v1`.
#
# Authored 2026-05-14 UTC by Stage 3.A migration auditor per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-14-usaspending-recipient-grain-lance-emit-v1.md
#
# Rollback summary (per audit invariant 10):
#   - s1 (Modal emit app code), s2 (tests) → `git revert <merge-SHA>`. PR is
#     monorepo single-PR; one revert covers both surfaces. Railway will NOT
#     auto-redeploy because s5 ban-list grep proves the PR had zero
#     apps/data-engine-x/app/** changes.
#   - s3 (Modal cron deploy) → `modal app stop` (idempotent). Cron suspends
#     immediately. Post-revert the next deploy of main will NOT redeploy this
#     app because the file is removed from the repo.
#   - s4 (Lance dataset write at production path) → IRREVERSIBLE but harmless.
#     The dataset lives at a fresh path that NO production code reads in
#     Phase 1 (audiences.py still queries the MV until Phase 2 ships). Treat
#     as "accept" — stale dataset on R2 causes zero behavior change. Phase 2
#     won't auto-fire because s4 parity gate failed (or won't fire at all if
#     the operator manually pulls Phase 1).
#
# Usage:
#   MERGE_SHA=<sha> ./usaspending-recipient-grain-lance-emit-v1-rollback.sh

set -euo pipefail

if [[ -z "${MERGE_SHA:-}" ]]; then
  echo "FAIL: MERGE_SHA env var required" >&2
  exit 2
fi

if [[ -n "${HQ_ALL_ROOT:-}" && -d "$HQ_ALL_ROOT/apps/data-engine-x" ]]; then
  :
else
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -d "$_root/apps/data-engine-x" ]]; then
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${HQ_ALL_ROOT:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout" >&2
  exit 2
fi

MODAL_APP_NAME="data-engine-x-usaspending-recipient-grain-lance-emit"

cat <<EOF
==> Rollback for usaspending-recipient-grain-lance-emit-v1 (MERGE_SHA=$MERGE_SHA)

Procedure:

  # 1. Stop the Modal cron (idempotent — safe to run before or after revert).
  modal app stop $MODAL_APP_NAME

  # 2. Revert the merge on origin/main.
  cd $HQ_ALL_ROOT
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
  git revert --no-edit $MERGE_SHA
  git push origin main

  # 3. Confirm Modal app is stopped (idempotent check).
  modal app list --json | jq -r '.[] | select(.Name == "$MODAL_APP_NAME") | .State'
  # Expected: "stopped" or no row at all.

NO Railway redeploy is triggered by this revert. Audit invariant s5
(ban-list grep proves zero apps/data-engine-x/app/** changes) means the
DEX FastAPI service is unaffected.

Lance dataset at s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance/
remains on disk after rollback. This is BY DESIGN — no Phase 1 code reads
it, and Phase 2 (consumer rewire) will not auto-fire. The stale dataset
is inert. If Operator wants to also delete the R2 prefix, run:

  doppler run --project hq-all --config prd -- bash -c "
    aws s3 rm --recursive \\
      --endpoint-url=\$R2_ENDPOINT \\
      s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance/
  "

But this is NOT required for rollback correctness.
EOF

echo ""
echo "==> Verifying revert pre-conditions:"

cd "$HQ_ALL_ROOT"
git fetch origin main >/dev/null 2>&1

if ! git cat-file -e "$MERGE_SHA^{commit}" 2>/dev/null; then
  echo "FAIL: MERGE_SHA $MERGE_SHA not found in local repo. Fetch first." >&2
  exit 1
fi

if ! git merge-base --is-ancestor "$MERGE_SHA" origin/main; then
  echo "FAIL: $MERGE_SHA is not an ancestor of origin/main — nothing to revert" >&2
  exit 1
fi

echo "==> Pre-conditions OK. Run the procedure above to execute the revert."
