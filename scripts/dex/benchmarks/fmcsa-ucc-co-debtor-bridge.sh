#!/usr/bin/env bash
# Benchmark: CO UCC debtor × FMCSA carrier identity Pattern B bridge baseline probe.
#
# Read-only input-side match estimate for the new
# bridges/fmcsa_ucc_co_debtor_lance bridge. Reads FMCSA carrier_essentials_lance
# (CO carriers — phy_state='CO') and ucc_co/debtors_lance (organization-type
# debtors — DEBTOR_TYPE='Organization', filtered to STATE='CO'), normalizes the
# legal name on BOTH sides via the SAME canonical normalizer the bridge will use
# (scripts/_lib/entity_name_normalize.normalize_entity_name, applied as a Python
# pre-pass — NOT a DuckDB SQL re-implementation), Arrow-bridges into DuckDB,
# INNER JOINs on the normalized legal name with both sides pinned to CO, applies
# the symmetric two-sided tier rule at COLLISION_THRESHOLD=50, and emits the
# dry-run match estimate + per-tier breakdown as JSON.
#
# CO counterpart of fmcsa-ucc-ca-debtor-bridge.sh (PR #619, shipped 2026-05-21) —
# identical FMCSA side, UCC side swapped ucc_ca/debtors_lance -> ucc_co/debtors_lance
# and the state filters swapped CA -> CO.
#
# Why the canonical Python normalizer (not SQL): the bridge template
# build_bridge_fmcsa_ucc_ca_debtor_lance.py normalizes legal names in Python via
# _lib/entity_name_normalize (its docstring + the normalizer module docstring
# both warn against the "DuckDB SQL approximation of the Python rule" drift trap
# — the 28-entry generic-string blacklist + suffix regex cannot be faithfully
# re-encoded inline). This benchmark uses the IDENTICAL code path so the estimate
# is a true forecast of the bridge's join key.
#
# Schema findings (validator-confirmed live PyLance scan, 2026-05-21):
#   - ucc_co/debtors_lance: 15-column CA-parity schema; 1,985,901 rows total.
#   - DEBTOR_TYPE distinct values are EXACTLY {'Organization': 938312,
#     'Individual': 1047589} — same 'Organization' literal as the CA dataset.
#   - STATE distinct count = 1; every row carries 'CO' (ucc_co is a CO-only
#     dataset). The STATE='CO' filter is therefore a structural no-op but is
#     kept for explicitness and CA-bridge parity (the CA bridge pins STATE='CA').
#   - fmcsa/carrier_essentials_lance: 4,435,694 rows total; phy_state='CO' -> 79,736.
#
# This is the VALIDATOR-scaffolded input-side estimate version. The executor
# extends it post-build to invoke `build_bridge_fmcsa_ucc_co_debtor_lance.py
# --dry-run` and assert the materialized dry-run count against the floor.
#
# Validator-created 2026-05-21. Measured runtime ~25-40s (well under <5 min).
#
# Floor: MIN_ROWS_MATCHED = 73000  (~54.4% of the validator-measured 134,196
#        non-rejected-row estimate, rounded down — conservative end of the
#        55-60% band because name+state matching is fuzzier than domain/UEI
#        joins. CO is a far smaller carrier base than CA (79,736 CO carriers
#        vs ~600k CA), so the absolute floor is proportionally smaller. The
#        estimate was deterministic across 3 runs (0% stddev). See directive
#        ## Success threshold + ## Validator notes.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/fmcsa-ucc-co-debtor-bridge.sh
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

# Probe Python is written to a temp file (not inlined via python3 -c) so the
# canonical-normalizer import survives the doppler `bash -c` wrapper's
# quote-stripping intact (CLAUDE.md §"Doppler shell gotcha").
PROBE_PY="$(mktemp -t fmcsa-ucc-co-debtor-probe.XXXXXX.py)"
trap 'rm -f "$PROBE_PY"' EXIT

cat > "$PROBE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Input-side match estimate for bridges/fmcsa_ucc_co_debtor_lance."""
import json
import os
import sys
import time

import duckdb
import lance
import pyarrow as pa

# Canonical normalizer — the SAME code path the bridge uses on BOTH sides.
# (cwd is apps/data-engine-x; scripts/ is importable.)
sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 73_000  # validator floor — see directive ## Success threshold

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
UCC_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance"

# v1 scope (directive C8): FMCSA side pinned to physical CO; UCC debtor side
# pinned to organization-type debtors that are themselves filed in CO. Both
# sides CO-pinned so the join key is (normalized_legal_name) with state already
# constrained — equivalent to a (normalized_legal_name, state='CO') join.
FMCSA_STATE = "CO"
UCC_STATE = "CO"
UCC_DEBTOR_TYPE = "Organization"  # validator confirmed live: distinct DEBTOR_TYPE
                                  # values are exactly {'Organization','Individual'}


def main():
    t0 = time.time()

    # FMCSA side — scan CO carriers, raw legal_name (re-normalized fresh below
    # via the canonical normalizer so both sides share one identical path).
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=SO)
    fmcsa_total = fmcsa_ds.count_rows()
    import pyarrow.compute as pc
    fmcsa_tbl = fmcsa_ds.scanner(
        columns=["dot_number", "legal_name", "phy_state"],
        filter=pc.field("phy_state") == FMCSA_STATE,
    ).to_table()
    fmcsa_co_rows = len(fmcsa_tbl)
    fmcsa_legal = fmcsa_tbl.column("legal_name").to_pylist()
    fmcsa_norm = [normalize_entity_name(n) for n in fmcsa_legal]
    fmcsa_tbl = fmcsa_tbl.append_column(
        "legal_name_normalized", pa.array(fmcsa_norm, type=pa.string())
    )

    # UCC debtor side — scan CO organization-type debtors, normalize ORG_NAME.
    ucc_ds = lance.dataset(UCC_LANCE_URI, storage_options=SO)
    ucc_total = ucc_ds.count_rows()
    ucc_tbl = ucc_ds.scanner(
        columns=["UCC1_NUM", "DEBTOR_TYPE", "ORG_NAME", "STATE"],
        filter=(pc.field("DEBTOR_TYPE") == UCC_DEBTOR_TYPE)
        & (pc.field("STATE") == UCC_STATE),
    ).to_table()
    ucc_co_org_rows = len(ucc_tbl)
    ucc_org = ucc_tbl.column("ORG_NAME").to_pylist()
    ucc_norm = [normalize_entity_name(n) for n in ucc_org]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized", pa.array(ucc_norm, type=pa.string())
    )

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.register("fmcsa", fmcsa_tbl)
    con.register("ucc", ucc_tbl)

    # Validated projections — drop None/empty normalized names on both sides.
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_proj AS
        SELECT dot_number, legal_name_normalized AS nn
        FROM fmcsa
        WHERE legal_name_normalized IS NOT NULL
          AND legal_name_normalized <> ''
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_proj AS
        SELECT UCC1_NUM AS ucc1_num, debtor_name_normalized AS nn
        FROM ucc
        WHERE debtor_name_normalized IS NOT NULL
          AND debtor_name_normalized <> ''
        """
    )
    rows_fmcsa_proj = con.execute("SELECT COUNT(*) FROM fmcsa_proj").fetchone()[0]
    rows_ucc_proj = con.execute("SELECT COUNT(*) FROM ucc_proj").fetchone()[0]
    distinct_fmcsa_names = con.execute(
        "SELECT COUNT(DISTINCT nn) FROM fmcsa_proj"
    ).fetchone()[0]
    distinct_ucc_names = con.execute(
        "SELECT COUNT(DISTINCT nn) FROM ucc_proj"
    ).fetchone()[0]

    # INNER JOIN on normalized legal name (both sides already CO-pinned).
    con.execute(
        """
        CREATE TEMP TABLE braw AS
        SELECT u.ucc1_num, f.dot_number, u.nn
        FROM ucc_proj u JOIN fmcsa_proj f ON u.nn = f.nn
        """
    )
    rows_raw = con.execute("SELECT COUNT(*) FROM braw").fetchone()[0]

    # Symmetric two-sided fan-out + tier rule at COLLISION_THRESHOLD.
    # Fan-out is per-SIDE: for a normalized name, ucc_fo = distinct UCC debtor
    # filings carrying it, fmcsa_fo = distinct FMCSA carriers carrying it.
    # Counting COUNT(*) over the join product would make both sides identical
    # (= the join-row count) and collapse the gold tier — measure each side's
    # distinct identity key independently.
    con.execute(
        "CREATE TEMP TABLE uf AS "
        "SELECT nn, COUNT(DISTINCT ucc1_num) AS ucc_fo FROM braw GROUP BY 1"
    )
    con.execute(
        "CREATE TEMP TABLE ff AS "
        "SELECT nn, COUNT(DISTINCT dot_number) AS fmcsa_fo FROM braw GROUP BY 1"
    )
    con.execute(
        f"""
        CREATE TEMP TABLE allr AS
        SELECT b.*, uf.ucc_fo, ff.fmcsa_fo,
          CASE
            WHEN uf.ucc_fo > {COLLISION_THRESHOLD}
              OR ff.fmcsa_fo > {COLLISION_THRESHOLD} THEN 'rejected'
            WHEN uf.ucc_fo = 1 AND ff.fmcsa_fo = 1 THEN 'platinum'
            WHEN uf.ucc_fo = 1 OR  ff.fmcsa_fo = 1 THEN 'gold'
            ELSE 'silver'
          END AS tier
        FROM braw b JOIN uf USING(nn) JOIN ff USING(nn)
        """
    )
    tiers = con.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE tier <> 'rejected'),
          COUNT(*) FILTER (WHERE tier = 'platinum'),
          COUNT(*) FILTER (WHERE tier = 'gold'),
          COUNT(*) FILTER (WHERE tier = 'silver'),
          COUNT(*) FILTER (WHERE tier = 'rejected'),
          MAX(ucc_fo), MAX(fmcsa_fo),
          COUNT(DISTINCT dot_number) FILTER (WHERE tier <> 'rejected'),
          COUNT(DISTINCT ucc1_num)   FILTER (WHERE tier <> 'rejected')
        FROM allr
        """
    ).fetchone()

    rows_matched = tiers[0]
    out = {
        "benchmark": "fmcsa-ucc-co-debtor-bridge",
        "mode": "input-side-estimate",
        "normalizer_version": NORMALIZER_VERSION,
        "collision_threshold": COLLISION_THRESHOLD,
        "fmcsa_carrier_essentials_total": fmcsa_total,
        "fmcsa_co_carriers": fmcsa_co_rows,
        "fmcsa_proj_validated_rows": rows_fmcsa_proj,
        "fmcsa_distinct_normalized_names": distinct_fmcsa_names,
        "ucc_debtors_total": ucc_total,
        "ucc_co_org_debtors": ucc_co_org_rows,
        "ucc_proj_validated_rows": rows_ucc_proj,
        "ucc_distinct_normalized_names": distinct_ucc_names,
        "raw_inner_join_rows": rows_raw,
        "est_rows_matched": rows_matched,
        "est_tier_platinum": tiers[1],
        "est_tier_gold": tiers[2],
        "est_tier_silver": tiers[3],
        "est_rows_rejected": tiers[4],
        "max_fan_out_ucc": tiers[5],
        "max_fan_out_fmcsa": tiers[6],
        "est_distinct_carriers_matched": tiers[7],
        "est_distinct_ucc_filings_matched": tiers[8],
        "min_rows_matched_floor": MIN_ROWS_MATCHED,
        "floor_met": rows_matched >= MIN_ROWS_MATCHED,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(out, indent=2))
    con.close()
    sys.exit(0 if out["floor_met"] else 1)


if __name__ == "__main__":
    main()
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$PROBE_PY'"

# ---------------------------------------------------------------------------
# Post-build gate (executor-extended per directive ## Benchmark).
# Invoke the materialized bridge's --dry-run and assert the count clears the
# floor. This verifies the live dataset, not the input-side estimate.
# Only runs if the bridge script exists (executor may run this benchmark before
# the bridge is built as well as after).
# ---------------------------------------------------------------------------
BRIDGE_SCRIPT="scripts/build_bridge_fmcsa_ucc_co_debtor_lance.py"
if [ -f "$BRIDGE_SCRIPT" ]; then
  echo ""
  echo "=== Post-build dry-run assertion ==="
  DRYRUN_OUT="$(doppler run --project hq-all --config prd -- \
    bash -c "uv run --project . python3 '$BRIDGE_SCRIPT' --dry-run" 2>&1)"
  echo "$DRYRUN_OUT"
  DRYRUN_ROWS=$(echo "$DRYRUN_OUT" | python3 -c \
    "import json,sys; lines=[l for l in sys.stdin if l.strip().startswith('{')]; \
     data=json.loads(''.join(lines[:20])) if lines else {}; \
     print(data.get('rows_matched', -1))" 2>/dev/null || echo -1)
  if [ "$DRYRUN_ROWS" -ge 73000 ] 2>/dev/null; then
    echo "POST-BUILD OK: bridge --dry-run rows_matched=${DRYRUN_ROWS} >= floor 73000"
    exit 0
  else
    echo "POST-BUILD FAIL: bridge --dry-run rows_matched=${DRYRUN_ROWS} < floor 73000" >&2
    exit 1
  fi
fi
