#!/usr/bin/env bash
# Benchmark: FMCSA carrier × CA Secretary-of-State entity identity Pattern B
# bridge baseline probe.
#
# Read-only input-side match estimate for the new
# bridges/fmcsa_sos_ca_owner_lance bridge. Reads FMCSA carrier_essentials_lance
# (CA carriers — phy_state='CA') and sos/ca_entities_lance (CA SoS business
# entities, ~9.4M rows), INNER JOINs on the normalized legal name, applies the
# symmetric two-sided tier rule at COLLISION_THRESHOLD=50, and emits the
# dry-run match estimate + per-tier breakdown as JSON.
#
# --- C3 (validator-resolved 2026-05-21) — DIVERGENT, asymmetric handling ---
# The single load-bearing pre-flight. By live PyLance scan + full-cohort
# parity check the validator established:
#
#   * CA SoS  ca_entities_lance.entity_name_normalized — 100.0000% parity with
#     canonical scripts/_lib/entity_name_normalize.normalize_entity_name v1.0.0
#     (204,125 / 204,125 sampled rows, 0 mismatches). It is PRODUCED by that
#     exact normalizer — run_ca_sos_master_unload_to_r2.py::_project_entities
#     calls normalize_entity_name(ENTITY_NAME) directly; the Lance emit
#     (run_ca_sos_entities_lance_emit.py) passes the column through verbatim.
#     => SoS side: join the pre-materialized entity_name_normalized DIRECTLY
#        (directive C3: "If confirmed identical, the bridge may join the
#        pre-materialized columns directly"). Re-normalizing 9.4M names in
#        Python would be pure cost for zero correctness gain.
#
#   * FMCSA  carrier_essentials_lance.legal_name_normalized — only 99.9786%
#     parity (495,382 / 495,488 CA rows; 106 mismatches). EVERY mismatch is
#     the same divergence class: the pre-materialized FMCSA column retains
#     single-character results ("o", "m", "k") and generic strings ("none",
#     "na", "n a", "not applicable", "self") that the canonical normalizer
#     v1.0.0 correctly rejects to NULL (its len(s)<2 guard + the
#     GENERIC_NON_ENTITY_STRINGS blacklist). The FMCSA column was built by an
#     older / blacklist-less normalizer revision. This is exactly the
#     terminal-divergence class that caused the PR #459/#460 reverts.
#     => FMCSA side: re-normalize the RAW legal_name fresh via the canonical
#        normalize_entity_name. NEVER trust the pre-materialized FMCSA column.
#
# Net: both join keys flow from canonical normalize_entity_name v1.0.0 — the
# SoS key was already produced by it; the FMCSA key is re-derived here. This is
# the identical discipline build_bridge_sba_sos_ca_owner_lance.py applies (raw
# SBA legal name re-normalized on the canonical path) and the identical FMCSA
# treatment in benchmarks/fmcsa-ucc-ca-debtor-bridge.sh.
#
# Why the canonical Python normalizer (not a DuckDB SQL re-impl): the 21-entry
# generic-string blacklist + the multi-token suffix regex + the len<2 guard
# cannot be faithfully re-encoded in inline SQL — the normalizer module
# docstring and every Pattern B bridge warn against the "DuckDB SQL
# approximation of the Python rule" drift trap. This benchmark imports the
# IDENTICAL code path so the estimate is a true forecast of the bridge's join
# key.
#
# --- C8 (validator-resolved 2026-05-21) — NO entity-status filter ---
# Every CA-SoS-entity bridge in the precedent family
# (build_bridge_sba_sos_ca_owner_lance.py — note that one joins ca_principals,
# build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py,
# build_bridge_ucc_ca_lender_sos_ca_owner_lance.py,
# build_bridge_usaspending_sos_ca_owner_lance.py,
# build_bridge_sba_sos_fl_owner_lance.py) applies exactly ONE filter on the SoS
# entity side: entity_name_normalized IS NOT NULL (the PyArrow is_valid()
# scanner filter). None of them filters on entity_status / standing_sos — a
# carrier may legitimately match a now-dissolved entity that remains the
# correct historical owner. entity_status + standing_sos are carried as
# downstream-filterable evidence columns, never as an exclusion. This benchmark
# matches against ALL CA SoS entities with a usable normalized name.
#
# This is the VALIDATOR-scaffolded input-side estimate version. The executor
# extends it post-build to invoke `build_bridge_fmcsa_sos_ca_owner_lance.py
# --dry-run` and assert the materialized dry-run count against the floor.
#
# Validator-created 2026-05-21. Measured runtime ~9-10s across 3 runs (well
# under the <5 min budget); estimate bit-identical all 3 runs (0% stddev).
#
# Floor: MIN_ROWS_MATCHED = 200000  (54.2% of the validator-measured 369,010
#        non-rejected-row estimate, rounded down to a clean integer —
#        conservative end of the 55-60% band, matching the cited CO UCC
#        precedent (73,000 / 134,196 = 54.4%) and CA UCC precedent
#        (300,000 / 545,143 = 55.0%). Name+state matching is fuzzier than
#        domain/UEI joins, so the floor sits at the low end of the band.
#        See directive ## Success threshold + ## Validator notes.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/fmcsa-sos-ca-owner-bridge.sh
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

# Probe Python is written to a temp file (not inlined via python3 -c) so the
# canonical-normalizer import survives the doppler `bash -c` wrapper's
# quote-stripping intact (CLAUDE.md §"Doppler shell gotcha").
PROBE_PY="$(mktemp -t fmcsa-sos-ca-owner-probe.XXXXXX.py)"
trap 'rm -f "$PROBE_PY"' EXIT

cat > "$PROBE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Input-side match estimate for bridges/fmcsa_sos_ca_owner_lance."""
import json
import os
import sys
import time

import duckdb
import lance
import pyarrow as pa
import pyarrow.compute as pc

# Canonical normalizer — the SAME code path the bridge uses on the FMCSA side
# (and the path that already produced the CA SoS entity_name_normalized column).
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
MIN_ROWS_MATCHED = 200_000  # validator floor — see directive ## Success threshold

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SOS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
)

# v1 scope (directive C8): FMCSA side pinned to physical CA. CA SoS side is the
# whole ca_entities grain (every CA-registered entity is CA by construction) —
# no entity-status filter, only entity_name_normalized IS NOT NULL. Both sides
# thus CA-pinned: the join key is (normalized_legal_name) with state already
# constrained — equivalent to a (normalized_legal_name, state='CA') join.
FMCSA_STATE = "CA"


def main():
    t0 = time.time()

    # FMCSA side — scan CA carriers, raw legal_name. Re-normalize fresh via the
    # canonical normalizer (C3: the pre-materialized FMCSA legal_name_normalized
    # column is divergent — 106/495,488 garbage names it fails to reject).
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=SO)
    fmcsa_total = fmcsa_ds.count_rows()
    fmcsa_tbl = fmcsa_ds.scanner(
        columns=["dot_number", "legal_name", "phy_state"],
        filter=pc.field("phy_state") == FMCSA_STATE,
    ).to_table()
    fmcsa_ca_rows = len(fmcsa_tbl)
    fmcsa_legal = fmcsa_tbl.column("legal_name").to_pylist()
    fmcsa_norm = [normalize_entity_name(n) for n in fmcsa_legal]
    fmcsa_tbl = fmcsa_tbl.append_column(
        "legal_name_normalized_canon", pa.array(fmcsa_norm, type=pa.string())
    )

    # CA SoS entity side — scan entities with a usable normalized name.
    # entity_name_normalized is 100% canonical (C3 — produced by
    # normalize_entity_name v1.0.0), so it is consumed DIRECTLY: is_valid()
    # at the scanner is the only filter (C8 — no entity-status exclusion).
    sos_ds = lance.dataset(SOS_LANCE_URI, storage_options=SO)
    sos_total = sos_ds.count_rows()
    sos_tbl = sos_ds.scanner(
        columns=["entity_num", "entity_name_normalized", "entity_status"],
        filter=pc.field("entity_name_normalized").is_valid(),
    ).to_table()
    sos_entity_rows = len(sos_tbl)

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("fmcsa", fmcsa_tbl)
    con.register("sos", sos_tbl)

    # Validated projections — drop None/empty normalized names on both sides.
    # (entity_name_normalized is already is_valid()-filtered at the scanner;
    # the empty-string guard is belt-and-suspenders.)
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_proj AS
        SELECT dot_number, legal_name_normalized_canon AS nn
        FROM fmcsa
        WHERE legal_name_normalized_canon IS NOT NULL
          AND legal_name_normalized_canon <> ''
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_proj AS
        SELECT entity_num, entity_name_normalized AS nn
        FROM sos
        WHERE entity_name_normalized IS NOT NULL
          AND entity_name_normalized <> ''
        """
    )
    rows_fmcsa_proj = con.execute("SELECT COUNT(*) FROM fmcsa_proj").fetchone()[0]
    rows_sos_proj = con.execute("SELECT COUNT(*) FROM sos_proj").fetchone()[0]
    distinct_fmcsa_names = con.execute(
        "SELECT COUNT(DISTINCT nn) FROM fmcsa_proj"
    ).fetchone()[0]
    distinct_sos_names = con.execute(
        "SELECT COUNT(DISTINCT nn) FROM sos_proj"
    ).fetchone()[0]

    # INNER JOIN on normalized legal name (both sides already CA-pinned).
    con.execute(
        """
        CREATE TEMP TABLE braw AS
        SELECT f.dot_number, s.entity_num, f.nn
        FROM fmcsa_proj f JOIN sos_proj s ON f.nn = s.nn
        """
    )
    rows_raw = con.execute("SELECT COUNT(*) FROM braw").fetchone()[0]

    # Symmetric two-sided fan-out + tier rule at COLLISION_THRESHOLD.
    # Fan-out is per-SIDE: for a normalized name, fmcsa_fo = distinct FMCSA
    # carriers carrying it, sos_fo = distinct SoS entities carrying it.
    # Counting COUNT(*) over the join product would make both sides identical
    # (= the join-row count) and collapse the gold tier — measure each side's
    # distinct identity key independently.
    con.execute(
        "CREATE TEMP TABLE ff AS "
        "SELECT nn, COUNT(DISTINCT dot_number) AS fmcsa_fo FROM braw GROUP BY 1"
    )
    con.execute(
        "CREATE TEMP TABLE sf AS "
        "SELECT nn, COUNT(DISTINCT entity_num) AS sos_fo FROM braw GROUP BY 1"
    )
    con.execute(
        f"""
        CREATE TEMP TABLE allr AS
        SELECT b.*, ff.fmcsa_fo, sf.sos_fo,
          CASE
            WHEN ff.fmcsa_fo > {COLLISION_THRESHOLD}
              OR sf.sos_fo > {COLLISION_THRESHOLD} THEN 'rejected'
            WHEN ff.fmcsa_fo = 1 AND sf.sos_fo = 1 THEN 'platinum'
            WHEN ff.fmcsa_fo = 1 OR  sf.sos_fo = 1 THEN 'gold'
            ELSE 'silver'
          END AS tier
        FROM braw b JOIN ff USING(nn) JOIN sf USING(nn)
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
          MAX(fmcsa_fo), MAX(sos_fo),
          COUNT(DISTINCT dot_number) FILTER (WHERE tier <> 'rejected'),
          COUNT(DISTINCT entity_num) FILTER (WHERE tier <> 'rejected')
        FROM allr
        """
    ).fetchone()

    rows_matched = tiers[0]
    out = {
        "benchmark": "fmcsa-sos-ca-owner-bridge",
        "mode": "input-side-estimate",
        "normalizer_version": NORMALIZER_VERSION,
        "collision_threshold": COLLISION_THRESHOLD,
        "fmcsa_carrier_essentials_total": fmcsa_total,
        "fmcsa_ca_carriers": fmcsa_ca_rows,
        "fmcsa_proj_validated_rows": rows_fmcsa_proj,
        "fmcsa_distinct_normalized_names": distinct_fmcsa_names,
        "sos_ca_entities_total": sos_total,
        "sos_entities_name_valid": sos_entity_rows,
        "sos_proj_validated_rows": rows_sos_proj,
        "sos_distinct_normalized_names": distinct_sos_names,
        "raw_inner_join_rows": rows_raw,
        "est_rows_matched": rows_matched,
        "est_tier_platinum": tiers[1],
        "est_tier_gold": tiers[2],
        "est_tier_silver": tiers[3],
        "est_rows_rejected": tiers[4],
        "max_fan_out_fmcsa": tiers[5],
        "max_fan_out_sos": tiers[6],
        "est_distinct_carriers_matched": tiers[7],
        "est_distinct_sos_entities_matched": tiers[8],
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
# Post-build assertion (executor-added — see directive ## Benchmark §4)
# ---------------------------------------------------------------------------
# After `build_bridge_fmcsa_sos_ca_owner_lance.py --apply` has materialized
# the Lance dataset, this section opens it directly and asserts the row count
# meets the MIN_ROWS_MATCHED floor. This is the "materialized dry-run count"
# check the contract requires the executor to add.
#
# Only runs if the Lance dataset already exists (skip on first pre-build run).
POST_PY="$(mktemp -t fmcsa-sos-ca-owner-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$PROBE_PY"' EXIT

cat > "$POST_PY" <<'POSTPYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify materialized fmcsa_sos_ca_owner_lance row count >= floor."""
import os
import sys

import lance

MIN_ROWS_MATCHED = 200_000  # must match build script + directive ## Success threshold

BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sos_ca_owner_lance"
)

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

try:
    ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=SO)
except Exception as e:
    print(f"SKIP: bridge dataset not yet materialized ({e})")
    sys.exit(0)  # pre-build run — not a failure

rows = ds.count_rows()
floor_met = rows >= MIN_ROWS_MATCHED
print(
    f"post-build assertion: rows={rows:,}  floor={MIN_ROWS_MATCHED:,}  "
    f"met={'YES' if floor_met else 'NO'}"
)

# Verify both BTREE indexes are present
indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
dot_ok = "dot_number" in idx_fields
entity_ok = "entity_num" in idx_fields
print(f"BTREE dot_number: {'OK' if dot_ok else 'MISSING'}")
print(f"BTREE entity_num: {'OK' if entity_ok else 'MISSING'}")

if not floor_met:
    print(f"FAIL: rows_matched={rows:,} < MIN_ROWS_MATCHED={MIN_ROWS_MATCHED:,}")
    sys.exit(1)
if not dot_ok or not entity_ok:
    print("FAIL: missing BTREE index(es)")
    sys.exit(1)

print("OK: post-build assertions passed")
sys.exit(0)
POSTPYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$POST_PY'"
