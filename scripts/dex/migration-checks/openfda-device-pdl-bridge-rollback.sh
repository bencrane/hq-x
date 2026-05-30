#!/usr/bin/env bash
# Rollback harness for /scope cycle openfda-device-pdl-bridge.
#
# Authored by the Stage 3.A audit subagent (2026-05-20 UTC).
#
# Runs in REVERSE order (s4 -> s3+s2+s1) so the operator can roll back the
# deploy surface independently or roll back the code surfaces as a group.
#
# Rollback model (per directive ## Surfaces table):
#   s4 (deploy)        — `modal app stop data-engine-x-openfda-device-pdl-bridge`.
#                        Brand-new app, no prior deployment — rollback is
#                        stop-the-app. Idempotent (a not-found app is acceptable).
#   s3 + s2 + s1 (code) — single `git revert <merge-SHA>` against main. All three
#                        code surfaces ship in one squash-merged PR; the
#                        squash-merge SHA reverts them as a unit.
#
# Required env:
#   MERGE_SHA   — the squash-merge SHA from `gh pr merge --squash` (needed for
#                 the s3+s2+s1 git-revert step).
#
# Optional env:
#   DRY_RUN=1   — print intended actions without running them.
#
# Usage:
#   MERGE_SHA=abc1234 ./openfda-device-pdl-bridge-rollback.sh
#   DRY_RUN=1 ./openfda-device-pdl-bridge-rollback.sh
#   ./openfda-device-pdl-bridge-rollback.sh --surface s4
#
# Rollback notes per surface:
#   s4 (Modal) — `modal app stop data-engine-x-openfda-device-pdl-bridge`. The
#                bridge Lance dataset in R2 (polaris-warehouse/bridges/
#                openfda_device_pdl_lance) and the ops.bridges /
#                ops.bridge_generation_runs rows STAY — harmless data + forensic
#                record. The operator may DELETE the Lance dataset manually via
#                `aws s3 rm --recursive` if storage pressure warrants.
#   s3+s2+s1   — `git revert <merge-SHA>` + push. The shared
#                company_name_state_exact v1.0.0 match-method version row is NOT
#                touched by this cycle (L21 — the s1 generator calls only
#                register_bridge), so no rollback is needed on it.
#
# ROLLBACK GATE: every surface has a satisfiable rollback (s4 = modal app stop;
# s1/s2/s3 = git revert of the squash-merge SHA). No surface is blocked-no-rollback.

set -euo pipefail

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

MODAL_APP_NAME='data-engine-x-openfda-device-pdl-bridge'

# --- CLI parsing -------------------------------------------------------- #
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    shift 2 ;;   # single-repo cycle — accepted + ignored
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

# ── s4: Modal — stop the bridge app ────────────────────────────────────── #
# Brand-new app; rollback is stop-the-app. `modal app stop` accepts the app
# name (validator confirmed `modal app stop --help` takes [APP_IDENTIFIER]).
# A not-found app is treated as already-rolled-back (idempotent).
if _should_run "s4"; then
  echo "-- s4: Modal stop app $MODAL_APP_NAME (idempotent — not-found is acceptable)"
  _run "doppler run --project hq-all --config prd -- bash -c '
    modal app stop \"$MODAL_APP_NAME\" 2>/tmp/openfda-pdl-stop.\$\$ || {
      if grep -qiE \"not found|no such app|could not find\" /tmp/openfda-pdl-stop.\$\$; then
        echo \"NOTE: app $MODAL_APP_NAME not found — already stopped / never deployed\"
      else
        cat /tmp/openfda-pdl-stop.\$\$ >&2
        rm -f /tmp/openfda-pdl-stop.\$\$
        exit 1
      fi
    }
    rm -f /tmp/openfda-pdl-stop.\$\$
  '"
  echo "-- s4: stop issued — verify with: modal app list --json"
fi

# ── s3 + s2 + s1: code surfaces — single `git revert <merge-SHA>` ──────── #
# All three code surfaces ship in the same squash-merged PR; one revert of the
# squash-merge SHA reverts them as a unit. `git revert --no-edit` auto-generates
# the revert commit message; the operator can amend if desired.
if _should_run "s3" || _should_run "s2" || _should_run "s1"; then
  echo "-- s3+s2+s1: git revert <MERGE_SHA> against $HQ_ALL_ROOT (single PR; reverts all three code surfaces as a unit)"
  if [[ -z "${MERGE_SHA:-}" ]]; then
    echo "   FAIL: MERGE_SHA unset; cannot revert" >&2
    exit 1
  fi
  _run "cd \"$HQ_ALL_ROOT\" && git fetch origin main && git checkout main && git pull --ff-only origin main && git revert --no-edit \"$MERGE_SHA\" && git push origin main"
  echo "-- s3+s2+s1: revert pushed to main (ops rows + bridge Lance dataset preserved for forensics)"
fi

echo ""
echo "==> Rollback complete. Verify with:"
echo "    $HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/openfda-device-pdl-bridge.sh"
echo "    (After the s3+s2+s1 revert, the verify harness's pre-deploy file gates will FAIL — expected.)"
