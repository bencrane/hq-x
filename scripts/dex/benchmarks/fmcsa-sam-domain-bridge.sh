#!/usr/bin/env bash
# Benchmark: FMCSA × SAM.gov domain Pattern B bridge — dry-run + C9 verification.
#
# Extends the validator-scaffolded input-side estimate (main-tree version at
# apps/data-engine-x/scripts/benchmarks/fmcsa-sam-domain-bridge.sh) to also
# invoke `build_bridge_fmcsa_sam_domain_lance.py --dry-run` and assert the
# materialized dry-run count against the floor. Post-build (executor) version.
#
# Domain normalization SQL is COPIED VERBATIM from
# scripts/build_bridge_sam_pdl_domain_lance.py (_normalize_domain_sql /
# _domain_validation_sql) — do not invent new normalization. The FMCSA side
# joins the pre-materialized email_domain_normalized column directly (built by
# scripts/build_fmcsa_carrier_essentials.py with the same rule) but still
# applies the shape-validation predicate for parity.
#
# Validator-scaffolded 2026-05-20; executor-extended 2026-05-21.
# Measured runtime: input-side estimate ~10s; dry-run ~18s; total <5 min.
#
# Floor: MIN_ROWS_MATCHED = 44000 (~59% of the validator-measured 74,804
#        estimate, rounded down — see directive ## Success threshold).
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/fmcsa-sam-domain-bridge.sh
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
SCRIPT_DIR="$REPO_ROOT/apps/data-engine-x"
cd "$SCRIPT_DIR"

MIN_ROWS_MATCHED=44000

echo "=== Phase 1: input-side estimate (Arrow-bridge DuckDB probe) ==="

# Probe Python is written to a temp file (not inlined via python3 -c) so the
# domain-normalization regex literals survive the doppler `bash -c` wrapper's
# quote-stripping intact.
PROBE_PY="$(mktemp -t fmcsa-sam-domain-probe.XXXXXX.py)"
trap 'rm -f "$PROBE_PY"' EXIT

cat > "$PROBE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Input-side match estimate for bridges/fmcsa_sam_domain_lance."""
import json
import os
import sys
import time

import duckdb
import lance

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 44_000  # validator floor — see directive ## Success threshold

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
USA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
)


def norm(expr):
    """Domain normalization SQL — VERBATIM from build_bridge_sam_pdl_domain_lance.py."""
    return (
        "regexp_replace(regexp_replace(regexp_replace("
        "lower(trim(" + expr + ")), '^https?://', ''"
        "), '^www\\.', ''"
        "), '/.*$', '')"
    )


def valid(col):
    """Validation predicate — VERBATIM from build_bridge_sam_pdl_domain_lance.py."""
    return (
        col + " ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$' "
        "AND NOT (" + col + " ~ '^[0-9.]+$')"
    )


def main():
    t0 = time.time()

    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=SO)
    fmcsa_tbl = fmcsa_ds.scanner(
        columns=["dot_number", "email_domain_normalized"]
    ).to_table()

    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=SO)
    sam_tbl = sam_ds.scanner(columns=["unique_entity_id", "entity_url"]).to_table()

    usa_ds = lance.dataset(USA_LANCE_URI, storage_options=SO)
    usa_tbl = usa_ds.scanner(columns=["recipient_uei"]).to_table()

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.register("fmcsa", fmcsa_tbl)
    con.register("sam", sam_tbl)
    con.register("usa", usa_tbl)

    # Validated projections + INNER JOIN + tiering
    con.execute(
        f"""
        CREATE TEMP TABLE fmcsa_proj AS
        SELECT dot_number, email_domain_normalized AS nd
        FROM fmcsa
        WHERE email_domain_normalized IS NOT NULL
          AND email_domain_normalized <> ''
          AND {valid('email_domain_normalized')}
        """
    )
    sam_url_norm = norm('"entity_url"')
    con.execute(
        f"""
        CREATE TEMP TABLE sam_proj AS
        WITH s AS (
          SELECT unique_entity_id AS uei, {sam_url_norm} AS nd
          FROM sam
          WHERE entity_url IS NOT NULL AND entity_url <> ''
        )
        SELECT uei, nd FROM s WHERE nd IS NOT NULL AND {valid('nd')}
        """
    )
    rows_fmcsa_proj = con.execute("SELECT COUNT(*) FROM fmcsa_proj").fetchone()[0]
    rows_sam_proj = con.execute("SELECT COUNT(*) FROM sam_proj").fetchone()[0]

    con.execute(
        """
        CREATE TEMP TABLE braw AS
        SELECT f.dot_number, s.uei, f.nd
        FROM fmcsa_proj f JOIN sam_proj s ON f.nd = s.nd
        """
    )
    rows_raw = con.execute("SELECT COUNT(*) FROM braw").fetchone()[0]

    con.execute(
        "CREATE TEMP TABLE ff AS SELECT nd, COUNT(*) AS fmcsa_fo FROM braw GROUP BY 1"
    )
    con.execute(
        "CREATE TEMP TABLE sf AS SELECT nd, COUNT(*) AS sam_fo FROM braw GROUP BY 1"
    )
    con.execute(
        f"""
        CREATE TEMP TABLE allr AS
        SELECT b.*, ff.fmcsa_fo, sf.sam_fo,
          CASE
            WHEN ff.fmcsa_fo > {COLLISION_THRESHOLD}
              OR sf.sam_fo > {COLLISION_THRESHOLD} THEN 'rejected'
            WHEN ff.fmcsa_fo = 1 AND sf.sam_fo = 1 THEN 'platinum'
            WHEN ff.fmcsa_fo = 1 OR  sf.sam_fo = 1 THEN 'gold'
            ELSE 'silver'
          END AS tier
        FROM braw b JOIN ff USING(nd) JOIN sf USING(nd)
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
          MAX(fmcsa_fo), MAX(sam_fo)
        FROM allr
        """
    ).fetchone()

    # C9 spine verification
    c9 = con.execute(
        """
        WITH kept AS (
          SELECT DISTINCT dot_number, uei FROM allr WHERE tier <> 'rejected'
        ),
        usa_uei AS (
          SELECT DISTINCT recipient_uei AS uei FROM usa
          WHERE recipient_uei IS NOT NULL AND recipient_uei <> ''
        )
        SELECT
          COUNT(DISTINCT k.dot_number) AS fed_winner_carriers,
          COUNT(DISTINCT k.uei) AS fed_winner_ueis
        FROM kept k JOIN usa_uei u ON k.uei = u.uei
        """
    ).fetchone()

    rows_matched = tiers[0]
    out = {
        "benchmark": "fmcsa-sam-domain-bridge",
        "mode": "input-side-estimate",
        "fmcsa_proj_validated_rows": rows_fmcsa_proj,
        "sam_proj_validated_rows": rows_sam_proj,
        "raw_inner_join_rows": rows_raw,
        "est_rows_matched": rows_matched,
        "est_tier_platinum": tiers[1],
        "est_tier_gold": tiers[2],
        "est_tier_silver": tiers[3],
        "est_rows_rejected": tiers[4],
        "max_fan_out_fmcsa": tiers[5],
        "max_fan_out_sam": tiers[6],
        "c9_federal_winner_carriers": c9[0],
        "c9_federal_winner_ueis": c9[1],
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

echo ""
echo "=== Phase 2: --dry-run via build_bridge_fmcsa_sam_domain_lance.py ==="
doppler run --project hq-all --config prd -- bash -c "
cd '$SCRIPT_DIR' && \
uv run --with duckdb --with pylance --with pyarrow --with 'psycopg[binary]' \
    python scripts/build_bridge_fmcsa_sam_domain_lance.py --dry-run
"

echo ""
echo "=== Benchmark complete ==="
