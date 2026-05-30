#!/usr/bin/env bash
# Rollback harness for /scope cycle ucc-ca-master-ingest.
#
# Runs surface rollbacks in REVERSE order (s18 → s17 → ... → s1). All
# changes are purely additive — forward-only migrations + IF NOT EXISTS +
# ON CONFLICT DO UPDATE — so the primary rollback path is:
#
#   git -C <hq-all-checkout> revert <merge-SHA>; git push  → Railway auto-redeploys.
#
# Per validator finding P7, Railway CLI v4.33.0 does NOT support
# `railway redeploy --deployment-id <id>`; the directive's documented
# rollback shape is wrong. The PRIMARY rollback path is git-revert-the-
# merge-commit + auto-on-merge redeploy. This harness prints the manual
# git-revert step for each code/migration surface and performs the best-
# effort R2 / Postgres cleanup that complements it.
#
# Accepts:
#   --surface <id>    roll back a single surface
#   --repo <name>     filter (single-repo cycle: only bencrane/hq-all)
#
# Sources the canonical helper at apps/data-engine-x/scripts/_lib/dex.sh.

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${HQ_ALL_ROOT:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi
# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"

# --- CLI parsing ---------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# --- R2 prefix rm helper ------------------------------------------------- #
# Usage: _r2_rm_prefix <prefix-relative-to-dex-raw-landing-zone>
_r2_rm_prefix() {
  local prefix="$1"
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://dex-raw-landing-zone/$prefix --recursive \
      --endpoint-url \$R2_ENDPOINT
  "
}

# REVERSE order — most-likely-to-need-rollback (smoke/deploy) first.

# ── s18: deploy-verifier runtime probe — no rollback action ────────────── #
# Probe is read-only. Nothing to roll back.
rollback_surface "s18" "bencrane/hq-all" '
  echo "no-op: deploy-verifier probe is read-only; rollback handled by s17."
'

# ── s17: Railway deploy — git-revert primary; no CLI deployment-id flag ── #
rollback_surface "s17" "bencrane/hq-all" '
  echo "manual (per validator P7 — Railway CLI v4.33.0 has NO --deployment-id flag):"
  echo "  Primary path: git -C $HQ_ALL_ROOT revert <merge-SHA>; git push origin main."
  echo "    Railway auto-on-merge redeploys the prior commit."
  echo "  Secondary (force redeploy current main): doppler run --project hq-all --config prd -- railway redeploy --service data-engine-x --yes"
  echo "  Informational (look up prior deployment id — but CLI cannot target it directly):"
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service data-engine-x --limit 2 --json | jq -r \".[1].id\""
'

# ── s16: classifier coverage check — read-side, no rollback ────────────── #
rollback_surface "s16" "bencrane/hq-all" '
  echo "no-op: coverage check is read-side over the smoke CSV; rollback handled by s13 + s7 + s10 reverts."
'

# ── s15: row-count plausibility — read-side, no rollback ───────────────── #
rollback_surface "s15" "bencrane/hq-all" '
  echo "no-op: plausibility check is read-side over Lance tables; rollback handled by s3-s7 reverts."
'

# ── s14: sanity-gate helper — git revert + (optional) delete CSV ────────── #
rollback_surface "s14" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes apps/data-engine-x/scripts/sanity_check_lender_pool.py)."
'

# ── s13: smoke CSV — operator-cleanable inventory artifact ─────────────── #
rollback_surface "s13" "bencrane/hq-all" '
  echo "manual: rm -f $HOME/Desktop/hq/inventory/ucc-ca-v1-lender-pool-2026-05-12.csv (inventory artifact, not in git)."
'

# ── s12: ops.data_sources FDIC + NCUA rows — UPDATE status=retired ─────── #
rollback_surface "s12" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes the seed migration)."
  echo "  Postgres cleanup (forward-only — set status=retired so audit history persists):"
  dex_psql_ddl "UPDATE ops.data_sources SET status='\''retired'\'', retired_at=NOW() WHERE display_name IN ('\''fdic_institutions_lance'\'','\''ncua_credit_unions_lance'\'') AND status='\''active'\''"
'

# ── s11: ops.data_sources ucc-ca row — UPDATE status=retired ───────────── #
rollback_surface "s11" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes the seed migration)."
  dex_psql_ddl "UPDATE ops.data_sources SET status='\''retired'\'', retired_at=NOW() WHERE display_name='\''ucc_ca_filings_lance'\'' AND status='\''active'\''"
'

# ── s10: classifier code — git revert ──────────────────────────────────── #
rollback_surface "s10" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes apps/data-engine-x/scripts/_lib/ucc_ca_classifier.py)."
'

# ── s9: NCUA seed — git revert + R2 cleanup ────────────────────────────── #
rollback_surface "s9" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes run_ncua_seed.py)."
  _r2_rm_prefix "polaris-warehouse/ncua/credit_unions_lance/" || true
  _r2_rm_prefix "ncua/credit_unions/" || true
'

# ── s8: FDIC seed — git revert + R2 cleanup ────────────────────────────── #
rollback_surface "s8" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes run_fdic_seed.py)."
  _r2_rm_prefix "polaris-warehouse/fdic/institutions_lance/" || true
  _r2_rm_prefix "fdic/institutions/" || true
'

# ── s7: lenders_lance derived — git revert + R2 cleanup ────────────────── #
rollback_surface "s7" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes build_ucc_ca_lenders_lance.py)."
  _r2_rm_prefix "polaris-warehouse/ucc_ca/lenders_lance/" || true
'

# ── s6: filing_amendments_lance — git revert + R2 cleanup ──────────────── #
rollback_surface "s6" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_ucc_ca_filing_amendments_lance.py)."
  _r2_rm_prefix "polaris-warehouse/ucc_ca/filing_amendments_lance/" || true
'

# ── s5: secured_parties_lance — git revert + R2 cleanup ────────────────── #
rollback_surface "s5" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_ucc_ca_secured_parties_lance.py)."
  _r2_rm_prefix "polaris-warehouse/ucc_ca/secured_parties_lance/" || true
'

# ── s4: debtors_lance — git revert + R2 cleanup ────────────────────────── #
rollback_surface "s4" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_ucc_ca_debtors_lance.py)."
  _r2_rm_prefix "polaris-warehouse/ucc_ca/debtors_lance/" || true
'

# ── s3: filings_lance — git revert + R2 cleanup ────────────────────────── #
rollback_surface "s3" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_ucc_ca_filings_lance.py)."
  _r2_rm_prefix "polaris-warehouse/ucc_ca/filings_lance/" || true
'

# ── s2: parsed parquet — R2 cleanup ────────────────────────────────────── #
rollback_surface "s2" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes parser/streaming-CSV-to-parquet script)."
  _r2_rm_prefix "ucc-ca/master/snapshot=2026-05-01/parsed/" || true
'

# ── s1: raw zip — R2 cleanup (preserves source of truth elsewhere) ─────── #
# The operator owns the local zip at /Users/benjamincrane/DataRequest...zip;
# R2 copy is forward-only restorable, no source-of-truth loss.
rollback_surface "s1" "bencrane/hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes upload_raw.py)."
  _r2_rm_prefix "ucc-ca/master/snapshot=2026-05-01/raw.zip" || true
'

echo ""
echo "Rollback complete (manual git-revert steps printed above; R2 + Postgres cleanup attempted)."
echo "Reminder: forward-only migration policy — re-apply after revert is idempotent (IF NOT EXISTS + ON CONFLICT)."
