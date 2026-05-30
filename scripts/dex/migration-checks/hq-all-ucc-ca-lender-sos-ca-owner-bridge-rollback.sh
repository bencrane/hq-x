#!/usr/bin/env bash
# Rollback harness for /scope cycle hq-all-ucc-ca-lender-sos-ca-owner-bridge.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC).
#
# Runs in REVERSE order (s5 → s4 → s3 → s2 → s1) so the operator can roll back
# each surface independently or as a group. Most surfaces roll back via
# `git revert <merge-SHA>` against the single PR; the deploy and Polaris
# surfaces require active intervention.
#
# Required env:
#   MERGE_SHA      — the squash-merge SHA from `gh pr merge --squash`.
#   PRIOR_DEPLOY_ID — the Railway deployment ID that preceded MERGE_SHA's
#                     deploy. Lookup:
#                       railway deployment list --service data-engine-x \
#                         --limit 2 --json | jq -r '.[1].id'
#
# Optional env:
#   DRY_RUN=1      — print intended actions without running them.
#
# Usage:
#   MERGE_SHA=abc1234 PRIOR_DEPLOY_ID=xyz ./hq-all-ucc-ca-lender-sos-ca-owner-bridge-rollback.sh
#   DRY_RUN=1 ./hq-all-ucc-ca-lender-sos-ca-owner-bridge-rollback.sh
#   ./hq-all-ucc-ca-lender-sos-ca-owner-bridge-rollback.sh --surface s4
#
# Rollback notes per surface:
#   s5 (Railway) — `railway redeploy --service data-engine-x --deployment-id <prior>`.
#   s4 (Polaris) — DELETE /api/catalog/polaris/v1/<catalog>/namespaces/bridges/
#                  generic-tables/ucc_ca_lender_sos_ca_owner_lance. Idempotent
#                  (HTTP 200/204/404 all acceptable).
#   s3 (lance_views.py edit) — `git revert <merge-SHA>` + push.
#   s2 (migration) — `git revert <merge-SHA>` + push. The ops.data_sources row
#                    intentionally stays (idempotent — re-applies cleanly on
#                    forward roll).
#   s1 (bridge code) — `git revert <merge-SHA>` + push. The ops.bridges and
#                      ops.bridge_generation_runs rows stay (forensic record).
#                      The Lance dataset in R2 also stays — it's harmless data;
#                      operator may DELETE manually via aws s3 rm --recursive
#                      if storage pressure warrants. The shared
#                      legal_name_state_exact_ca v1.0.0 match method version
#                      row is NOT touched by this cycle (validator p4 / L21),
#                      so no rollback is needed on it.

set -uo pipefail

# --- locate canonical hq-all checkout ----------------------------------- #
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

# --- CLI parsing -------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

DRY_RUN="${DRY_RUN:-0}"
_run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY_RUN: $*"
    return 0
  fi
  eval "$@"
}

_should_run() {
  local id="$1"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id: SKIPPED (surface filter)"
    return 1
  fi
  return 0
}

# ── s5: Railway data-engine-x — redeploy prior deployment ───────────── #
if _should_run "s5"; then
  echo "-- s5: Railway rollback to prior deployment"
  if [[ -z "${PRIOR_DEPLOY_ID:-}" ]]; then
    echo "   PRIOR_DEPLOY_ID unset; attempting lookup via railway CLI ..."
    PRIOR_DEPLOY_ID=$(
      doppler run --project hq-all --config prd -- bash -c \
        "railway deployment list --service data-engine-x --limit 2 --json | jq -r '.[1].id'"
    ) || {
      echo "   FAIL: could not look up PRIOR_DEPLOY_ID — set it manually and retry" >&2
      exit 1
    }
    echo "   PRIOR_DEPLOY_ID=$PRIOR_DEPLOY_ID (from CLI lookup)"
  fi
  _run "doppler run --project hq-all --config prd -- bash -c \"railway redeploy --service data-engine-x --deployment-id '$PRIOR_DEPLOY_ID'\""
  echo "-- s5: deploy redeploy issued (verify with: railway deployment list --service data-engine-x --limit 1 --json)"
fi

# ── s4: Polaris — DELETE the generic table registration ──────────────── #
if _should_run "s4"; then
  echo "-- s4: Polaris DELETE bridges/ucc_ca_lender_sos_ca_owner_lance (idempotent: 200/204/404 acceptable)"
  _run "doppler run --project hq-all --config prd -- bash -c '
    set -euo pipefail
    base_url=\${POLARIS_PUBLIC_URL%/}
    catalog=\$POLARIS_DEFAULT_CATALOG_NAME
    token=\$(curl -s -X POST \"\$base_url/api/catalog/v1/oauth/tokens\" \\
      -d grant_type=client_credentials \\
      -d client_id=\"\$POLARIS_ROOT_PRINCIPAL_ID\" \\
      -d client_secret=\"\$POLARIS_ROOT_PRINCIPAL_SECRET\" \\
      -d scope=PRINCIPAL_ROLE:ALL | jq -r .access_token)
    [ -n \"\$token\" ] && [ \"\$token\" != null ] || { echo FAIL: polaris token; exit 1; }
    code=\$(curl -s -o /tmp/polaris-del.\$\$ -w \"%{http_code}\" -X DELETE \\
      -H \"Authorization: Bearer \$token\" \\
      \"\$base_url/api/catalog/polaris/v1/\$catalog/namespaces/bridges/generic-tables/ucc_ca_lender_sos_ca_owner_lance\")
    rm -f /tmp/polaris-del.\$\$
    case \"\$code\" in
      200|204|404) echo PASS: polaris delete returned \$code ;;
      *) echo FAIL: polaris delete returned \$code >&2; exit 1 ;;
    esac
  '"
fi

# ── s3, s2, s1: code/migration/bridge code — single `git revert <merge-SHA>` ── #
# All three surfaces ship in the same PR; the squash-merge SHA reverts them as
# a unit. We invoke `git revert --no-edit` so the revert commit message is
# auto-generated; the operator can amend if desired.
if _should_run "s3" || _should_run "s2" || _should_run "s1"; then
  echo "-- s3+s2+s1: git revert <MERGE_SHA> against $HQ_ALL_ROOT (single PR; reverts all three code/migration surfaces as a unit)"
  if [[ -z "${MERGE_SHA:-}" ]]; then
    echo "   FAIL: MERGE_SHA unset; cannot revert" >&2
    exit 1
  fi
  _run "cd \"$HQ_ALL_ROOT\" && git fetch origin main && git checkout main && git pull --ff-only origin main && git revert --no-edit \"$MERGE_SHA\" && git push origin main"
  echo "-- s3+s2+s1: revert pushed to main (Railway auto-redeploys; ops rows + Lance dataset preserved for forensics)"
fi

echo ""
echo "==> Rollback complete. Verify with:"
echo "    POPULATED=1 MERGE_SHA=<revert-SHA> $HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh"
echo "    (After revert, the verify harness's pre-deploy file gates will FAIL — expected.)"
