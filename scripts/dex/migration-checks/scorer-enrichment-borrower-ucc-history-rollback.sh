#!/usr/bin/env bash
# Rollback harness for /scope cycle scorer-enrichment-borrower-ucc-history.
#
# Surface rollback strategies:
#   s1, s2, s3, s7 — git revert <merge-SHA> (all NEW files or additive edits)
#   s4             — UPDATE ops.data_sources SET status='retired' (preserves FK refs)
#   s5             — UPDATE-back to PRE-state JSONB (captured in migration comment)
#   s6             — Polaris DELETE /api/catalog/.../namespaces/borrowers/generic-tables/ucc_profile_lance
#   s8             — aws s3 rm s3://dex-raw-landing-zone/polaris-warehouse/borrowers/ucc_profile_lance/ --recursive
#   s9, s10        — railway redeploy to prior deployment ID
#   s11            — smoke is read-only; rollback IS the underlying code/data revert
#
# Usage:
#   bash scorer-enrichment-borrower-ucc-history-rollback.sh --surface s4
#   bash scorer-enrichment-borrower-ucc-history-rollback.sh --surface s5

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
if [[ -z "${HQ_ALL_ROOT:-}" ]]; then
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${HQ_ALL_ROOT:-}" ]] || [[ ! -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi
export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}

# --- CLI parsing ---------------------------------------------------------- #
SURFACE_TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_TARGET="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$SURFACE_TARGET" ]]; then
  echo "Usage: $0 --surface <id>" >&2
  exit 2
fi

echo "==> Rollback: surface=$SURFACE_TARGET"

case "$SURFACE_TARGET" in

  s1|s2|s3|s7)
    echo "-- $SURFACE_TARGET: rollback = git revert <merge-SHA> + push"
    echo "   (file is NEW or additive edit; revert removes it; IF NOT EXISTS / None default makes re-apply safe)"
    echo "   Run: git revert <merge-SHA> && git push origin main"
    ;;

  s4)
    echo "-- s4: retiring ops.data_sources row for borrowers_ucc_profile_lance"
    doppler run --project hq-all --config prd -- bash -c '
      psql "$DEX_DB_URL_DIRECT" -c "
        UPDATE ops.data_sources
        SET status = '"'"'retired'"'"'
        WHERE display_name = '"'"'borrowers_ucc_profile_lance'"'"';
      "
    '
    echo "-- s4: rollback applied (status=retired)"
    ;;

  s5)
    echo "-- s5: reverting capital_partner_bridge_match_v1 scoring_strategy to PRE-state"
    # PRE-state captured verbatim from live SELECT 2026-05-13 (see migration comment):
    # {"scalar_weight":1.0,"vector_weight":0.5,
    #  "bridge_tier_bonus":{"gold":0.15,"silver":0.0,"platinum":0.3},
    #  "recency_boost_weight":0.3}
    _hqx_doppler '
      psql "$HQX_DB_URL_DIRECT" -c "
        UPDATE business.matching_relationships
        SET scoring_strategy = '"'"'{
          \"scalar_weight\": 1.0,
          \"vector_weight\": 0.5,
          \"bridge_tier_bonus\": {\"gold\": 0.15, \"silver\": 0.0, \"platinum\": 0.3},
          \"recency_boost_weight\": 0.3
        }'"'"'::jsonb
        WHERE name = '"'"'capital_partner_bridge_match_v1'"'"';
      "
    '
    echo "-- s5: rollback applied (flat bridge_tier_bonus shape restored)"
    ;;

  s6)
    echo "-- s6: deleting Polaris borrowers/ucc_profile_lance registration"
    doppler run --project hq-all --config prd -- bash -c '
      curl -fsS -X DELETE \
        -H "Authorization: Bearer $POLARIS_TOKEN" \
        "$POLARIS_BASE_URL/api/catalog/polaris/v1/$POLARIS_PREFIX/namespaces/borrowers/generic-tables/ucc_profile_lance" \
        -w "\nHTTP %{http_code}\n" || true
    '
    echo "-- s6: rollback applied (404-on-already-deleted = success)"
    ;;

  s8)
    echo "-- s8: deleting R2 prefix for borrowers/ucc_profile_lance"
    doppler run --project hq-all --config prd -- bash -c '
      aws s3 rm s3://dex-raw-landing-zone/polaris-warehouse/borrowers/ucc_profile_lance/ \
        --recursive \
        --endpoint-url "$R2_ENDPOINT" 2>&1
    '
    echo "-- s8: rollback applied (R2 prefix deleted)"
    ;;

  s9)
    echo "-- s9: rollback hq-x to prior Railway deployment"
    echo "   Lookup prior: railway deployment list --service hq-x --limit 2 --json | jq -r '.[1].id'"
    echo "   Then run:     railway redeploy --service hq-x --deployment-id <prior-id>"
    ;;

  s10)
    echo "-- s10: rollback data-engine-x to prior Railway deployment"
    echo "   Lookup prior: railway deployment list --service data-engine-x --limit 2 --json | jq -r '.[1].id'"
    echo "   Then run:     railway redeploy --service data-engine-x --deployment-id <prior-id>"
    ;;

  s11)
    echo "-- s11: smoke is read-only — rollback IS the underlying code/data revert"
    echo "   Run s1-s10 rollbacks as needed to restore pre-cycle state."
    ;;

  *)
    echo "Unknown surface: $SURFACE_TARGET" >&2
    exit 2
    ;;

esac

echo "==> Rollback complete for surface=$SURFACE_TARGET"
