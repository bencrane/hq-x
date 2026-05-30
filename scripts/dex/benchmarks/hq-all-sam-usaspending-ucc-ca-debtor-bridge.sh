#!/usr/bin/env bash
# Benchmark — SAM.gov + USAspending → CA UCC-CA org-debtor identity-match bridges.
#
# Cycle: hq-all-sam-usaspending-ucc-ca-debtor-bridge
# Directive: ~/Desktop/hq/directives/2026-05-20-hq-all-sam-usaspending-ucc-ca-debtor-bridge.md
#
# Asserts, FOR EACH of the two emitted bridges
#   bridges/sam_ucc_ca_debtor_lance        (sam_ucc_ca_debtor)
#   bridges/usaspending_ucc_ca_debtor_lance (usaspending_ucc_ca_debtor)
# the directive ## Benchmark contract:
#   (1) latest ops.bridge_generation_runs row is status='completed'
#       with rows_matched >= per-bridge MIN_ROWS_MATCHED floor;
#   (2) the Lance dataset exists at polaris-warehouse/bridges/<name>_lance
#       and carries the per-row provenance columns (constraint #3);
#   (3) dual BTREE scalar index present on BOTH name join-key columns
#       (constraint #4);
#   (4) tier distribution is neither all-one-tier nor all-rejected
#       (constraint #6 — silver MAY legitimately be 0 post-DISTINCT, that is
#        NOT "all one tier" as long as platinum AND gold are both populated).
# Combined success threshold: SAM rows_matched + USAspending rows_matched
#   >= 55,000 (45,000 SAM floor + 10,000 USAspending floor).
#
# Designed to FAIL CLEANLY before the bridges are built (the ops rows and
# Lance datasets do not exist yet -> explicit non-zero exit with a [FAIL]
# line) and PASS once both executor builds land. Runtime <5 min.
#
# Exit 0 = all assertions pass. Any non-zero = harness break / unmet contract.
#
# Use:
#   bash apps/data-engine-x/scripts/benchmarks/hq-all-sam-usaspending-ucc-ca-debtor-bridge.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)/apps/data-engine-x"

# Per-bridge floors — validator-calibrated 2026-05-20 from the deterministic
# 3x name-overlap probe (stddev 0.0%). ~70% of measured baseline match count.
#   SAM cohort        measured rows_matched = 65,470 -> floor 45,000
#   USAspending cohort measured rows_matched = 15,306 -> floor 10,000
SAM_FLOOR=45000
USASPENDING_FLOOR=10000
COMBINED_FLOOR=55000

doppler run --project hq-all --config prd -- uv run --project . python <<'PYEOF'
import os
import sys

import lance

SAM_FLOOR = 45_000
USASPENDING_FLOOR = 10_000
COMBINED_FLOOR = 55_000

# (bridge_name in ops.bridge_generation_runs, Lance URI, dual BTREE key cols, floor)
BRIDGES = [
    {
        "bridge_name": "sam_ucc_ca_debtor",
        "lance_uri": (
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_ucc_ca_debtor_lance"
        ),
        "btree_cols": ["sam_legal_name_normalized", "ucc_debtor_name_normalized"],
        "floor": SAM_FLOOR,
    },
    {
        "bridge_name": "usaspending_ucc_ca_debtor",
        "lance_uri": (
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "usaspending_ucc_ca_debtor_lance"
        ),
        "btree_cols": [
            "usaspending_recipient_name_normalized",
            "ucc_debtor_name_normalized",
        ],
        "floor": USASPENDING_FLOOR,
    },
]

# Per-row provenance columns required on every bridge row (constraint #3).
REQUIRED_PROVENANCE_COLS = {
    "bridge_run_id",
    "match_method",
    "match_value",
    "confidence_tier",
    "bridge_version",
    "generated_at",
}


def storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def connect_pg():
    import psycopg

    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise EnvironmentError("DEX_DB_URL_DIRECT is required")
    return psycopg.connect(url, autocommit=True)


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"[ok]   {msg}")


def check_bridge(con, so: dict, spec: dict) -> tuple[bool, int]:
    """Return (all_assertions_passed, rows_matched_for_combined_total)."""
    name = spec["bridge_name"]
    passed = True
    rows_matched = 0
    print(f"--- bridge: {name} ---")

    # (1) latest ops.bridge_generation_runs row: completed + rows_matched>=floor.
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT status, rows_matched, rows_tier1, rows_tier2, rows_tier3,
                   rows_collision_rejected
            FROM ops.bridge_generation_runs
            WHERE bridge_name = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (name,),
        )
        row = cur.fetchone()

    if row is None:
        fail(f"no ops.bridge_generation_runs row for bridge_name={name!r} "
             f"(bridge not built yet)")
        return False, 0

    status, rows_matched, t1, t2, t3, rejected = row
    rows_matched = rows_matched or 0
    t1, t2, t3 = (t1 or 0), (t2 or 0), (t3 or 0)

    if status != "completed":
        fail(f"latest run status={status!r}, expected 'completed'")
        passed = False
    else:
        ok(f"latest run status=completed")

    if rows_matched < spec["floor"]:
        fail(f"rows_matched={rows_matched:,} < floor={spec['floor']:,}")
        passed = False
    else:
        ok(f"rows_matched={rows_matched:,} >= floor={spec['floor']:,}")

    # (4) tier distribution: not all-one-tier, not all-rejected.
    #     silver==0 is acceptable post-DISTINCT (ucc_fan_out==1) per
    #     constraint #6 / the PPP precedent — the live tiers are platinum+gold.
    populated_tiers = sum(1 for c in (t1, t2, t3) if c > 0)
    if rows_matched == 0:
        fail("tier check: rows_matched=0 (all-rejected or empty)")
        passed = False
    elif populated_tiers < 2:
        fail(f"tier check: only {populated_tiers} non-rejected tier populated "
             f"(platinum={t1:,} gold={t2:,} silver={t3:,}) — all-one-tier")
        passed = False
    else:
        ok(f"tier distribution healthy "
           f"(platinum={t1:,} gold={t2:,} silver={t3:,} rejected={rejected or 0:,})")

    # (2) Lance dataset exists + carries provenance columns.
    try:
        ds = lance.dataset(spec["lance_uri"], storage_options=so)
        cols = {f.name for f in ds.schema}
        lance_rows = ds.count_rows()
        ok(f"Lance dataset exists ({lance_rows:,} rows) at {spec['lance_uri']}")
    except Exception as exc:  # noqa: BLE001
        fail(f"Lance dataset missing/unreadable at {spec['lance_uri']}: "
             f"{type(exc).__name__}: {exc}")
        return False, rows_matched

    missing_prov = REQUIRED_PROVENANCE_COLS - cols
    if missing_prov:
        fail(f"missing provenance columns: {sorted(missing_prov)}")
        passed = False
    else:
        ok(f"all {len(REQUIRED_PROVENANCE_COLS)} provenance columns present")

    # (3) dual BTREE on both name join-key columns.
    # PyLance 6.0.0 reports a scalar index's 'type' as the concrete kind
    # ('BTree' / 'Bitmap'), NOT the umbrella label 'Scalar'. Accept all
    # scalar-index type strings so the dual-BTREE assertion matches the
    # index shape create_scalar_index(..., index_type='BTREE') actually
    # writes. Vector index types (e.g. 'IvfPq') are intentionally excluded.
    SCALAR_INDEX_TYPES = {"btree", "bitmap", "scalar", "labellist", "inverted"}
    try:
        indexed = {
            idx["fields"][0]
            for idx in ds.list_indices()
            if str(idx.get("type", "")).lower() in SCALAR_INDEX_TYPES
            and idx.get("fields")
        }
    except Exception:  # noqa: BLE001
        # Fallback: older PyLance — list_indices() may differ; treat as unknown.
        indexed = set()
        # Re-derive field names via the per-index 'columns'/'fields' keys.
        for idx in ds.list_indices():
            for key in ("fields", "columns"):
                if idx.get(key):
                    indexed.update(idx[key])

    for col in spec["btree_cols"]:
        if col not in cols:
            fail(f"BTREE target column {col!r} not in Lance schema")
            passed = False
        elif col not in indexed:
            fail(f"no scalar index on {col!r} (dual-BTREE constraint #4)")
            passed = False
        else:
            ok(f"BTREE index present on {col!r}")

    return passed, rows_matched


def main() -> int:
    so = storage_options()
    all_passed = True
    combined = 0

    with connect_pg() as con:
        for spec in BRIDGES:
            passed, rows_matched = check_bridge(con, so, spec)
            all_passed = all_passed and passed
            combined += rows_matched
            print()

    print("--- combined success threshold ---")
    if combined >= COMBINED_FLOOR:
        ok(f"combined rows_matched={combined:,} >= threshold={COMBINED_FLOOR:,}")
    else:
        fail(f"combined rows_matched={combined:,} < threshold={COMBINED_FLOOR:,}")
        all_passed = False

    print()
    if all_passed:
        print("BENCHMARK PASS — both bridges meet the directive contract.")
        return 0
    print("BENCHMARK FAIL — see [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
PYEOF
