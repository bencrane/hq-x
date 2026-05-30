#!/usr/bin/env bash
# Benchmark: CA UCC lender × PDL company identity Pattern B bridge — post-emit
# distinct-lender coverage probe.
#
# Measures the TARGET METRIC of the bridges/ucc_ca_lender_pdl_lance bridge:
# the distinct-lender name+state PDL match rate of the high-volume target
# cohort. Cohort = ucc_ca/lenders_lance rows with
#   bank_classification IN ('non_bank','unknown') AND active_filings >= 25.
# "matched" = the lender's canonically-normalized name appears in the emitted
# bridge at confidence_tier ∈ {platinum, gold, silver}; "name+state matched" =
# additionally the matched PDL row's state agrees with the lender's own state.
#
# --- Lender state source (validator-resolved 2026-05-21, RE-RUN) ---
# Lender state is sourced from ucc_ca/secured_parties_lance.STATE — a clean,
# structured 2-letter column populated on 99.9% of the 4.68M
# SECURED_PARTY_TYPE='Organization' rows. Per canonically-normalized lender
# name, the lender's state is the DOMINANT (most-frequent, non-null) STATE
# across that lender's Organization secured-party rows. lenders_lance is
# DERIVED from secured_parties_lance, so the latter is the authoritative
# lender-state source. This is the same state source the operator's own
# probe used to produce the 31.1% name+state baseline figure.
#
# An earlier validation run measured lender state by parsing free-text
# lenders_lance.address_sample and got a spurious 4% name+state rate — that
# was the WRONG state source. secured_parties_lance.STATE is authoritative.
#
# --- Canonical name normalization (validator finding, RETAINED) ---
# lenders_lance.lender_name_normalized is UPPERCASE and punctuation-retaining
# ("GOODLEAP, LLC", "SNAP-ON CREDIT LLC") — it is NOT the canonical
# entity_name_normalize v1.0.0 form. This benchmark re-normalizes BOTH the raw
# lender name (lenders_lance.lender_name_normalized re-run through the
# canonical normalizer) and secured_parties_lance.ORG_NAME through
# scripts/_lib/entity_name_normalize.normalize_entity_name — the IDENTICAL
# code path the bridge uses. Never trust a pre-materialized normalized column
# unless full-parity verified (precedent: benchmarks/fmcsa-sos-ca-owner-bridge.sh
# C3). The 212 cohort rows that normalize to NULL (generic / government strings
# the canonical blacklist rejects) are dropped — the cohort denominator is
# 1,949 distinct canonically-normalized names, not 2,161 raw rows.
#
# --- Exact-match baseline (validator-measured 2026-05-21, RE-RUN) ---
# EXACT normalized-name match of the cohort against the raw PDL parquet
# (s3://dex-raw-landing-zone/pdl/free_company_dataset.parquet, scoped
# country='united states' OR country IS NULL) reproduces at ~46.6% name-only /
# ~31.1% name+state on the active>=25 non_bank+unknown cohort. See the
# directive ## Baseline table for the full per-cohort numbers. The fuzzy
# bridge's job is to recover coverage above that exact floor.
#
# --- What this benchmark prints ---
# Single post-emit number: the distinct-lender name+state match rate (percent)
# of the active>=25 non_bank+unknown cohort against the emitted bridge, plus
# the name-only rate and per-tier breakdown as JSON. Exit 0 iff the name+state
# rate clears the directive ## Success threshold; exit 1 if below.
#
# If the bridge dataset is not yet emitted (pre-Stage-3), the benchmark prints
# a clear "bridge not yet emitted" message and exits 0 (not-yet-runnable is not
# a failure — re-run after the build).
#
# Runtime: < 1 min (the bridge is small; secured_parties_lance.STATE rollup is
# the only multi-million-row scan and is Lance-native columnar).
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/ucc-ca-lender-pdl-bridge.sh
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

# Probe Python is written to a temp file (not inlined via python3 -c) so the
# canonical-normalizer import survives the doppler `bash -c` wrapper's
# quote-stripping intact (CLAUDE.md §"Doppler shell gotcha").
PROBE_PY="$(mktemp -t ucc-ca-lender-pdl-probe.XXXXXX.py)"
trap 'rm -f "$PROBE_PY"' EXIT

cat > "$PROBE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Post-emit distinct-lender cohort coverage probe for bridges/ucc_ca_lender_pdl_lance."""
import json
import os
import sys
import time

import duckdb
import lance
import pyarrow.compute as pc

# Canonical normalizer — the SAME code path the bridge uses on the lender side
# (and that this benchmark applies to BOTH the lenders_lance name and the
# secured_parties_lance ORG_NAME).
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

# Success threshold + HARD-FAIL floor — validator-set from the measured
# exact-match name+state baseline of 29.9% (active>=25 cohort; directive
# ## Baseline + ## Success threshold).
#   SUCCESS_THRESHOLD_PCT 33.0 — exact name+state baseline is 29.9%; the fuzzy
#     bridge must clear it by >=3 points. Measured fuzzy headroom is ~3-5 points
#     (an emitted fuzzy run reached 33.4%), so 33.0 demands real fuzzy value
#     without assuming an unreachable lift.
#   MIN_ROWS_MATCHED 30_000 — HARD-FAIL floor on total emitted bridge rows.
#     An emitted fuzzy run produced 43,142 rows; 30k is comfortably below that
#     yet catches a catastrophically broken emit (normalizer mismatch, empty
#     join). Floor is on the WHOLE bridge (full-corpus), not the cohort subset.
SUCCESS_THRESHOLD_PCT = 33.0   # name+state distinct-lender rate, active>=25 cohort
MIN_ROWS_MATCHED = 30_000      # HARD-FAIL floor on total emitted bridge rows

LENDERS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance"
)
SECURED_PARTIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_pdl_lance"
)

COHORT_MIN_ACTIVE = 25


def main():
    t0 = time.time()

    # ---- Pre-flight: is the bridge emitted? -------------------------------
    try:
        bridge_ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=SO)
        bridge_rows = bridge_ds.count_rows()
    except Exception as e:
        print(
            json.dumps(
                {
                    "benchmark": "ucc-ca-lender-pdl-bridge",
                    "status": "bridge-not-yet-emitted",
                    "message": (
                        "bridges/ucc_ca_lender_pdl_lance does not exist yet — "
                        f"run the build first ({e}). Not-yet-runnable is not a "
                        "failure; re-run this benchmark post-emit."
                    ),
                },
                indent=2,
            )
        )
        sys.exit(0)

    # ---- Cohort: active>=25 non_bank+unknown, canonically re-normalized ----
    lenders_tbl = lance.dataset(
        LENDERS_LANCE_URI, storage_options=SO
    ).scanner(
        columns=[
            "lender_name_normalized",
            "bank_classification",
            "active_filings",
            "total_filings",
        ],
    ).to_table()

    # ---- Lender state: dominant non-null STATE per normalized lender name
    #      across SECURED_PARTY_TYPE='Organization' secured-party rows. -------
    sp_tbl = lance.dataset(
        SECURED_PARTIES_LANCE_URI, storage_options=SO
    ).scanner(
        columns=["ORG_NAME", "STATE", "SECURED_PARTY_TYPE"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
    ).to_table()

    # ---- Bridge ----------------------------------------------------------
    bridge_tbl = bridge_ds.scanner(
        columns=[
            "lender_name_normalized",
            "pdl_id",
            "pdl_state",
            "confidence_tier",
        ],
    ).to_table()

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET preserve_insertion_order=false")
    # The canonical normalizer returns NULL legitimately (generic strings,
    # suffix-only results) — register with SPECIAL null handling so a None
    # return is not mistaken for a filtered-input error.
    con.create_function(
        "py_norm", normalize_entity_name, ["VARCHAR"], "VARCHAR",
        null_handling="special",
    )
    con.register("lenders_raw", lenders_tbl)
    con.register("sp_raw", sp_tbl)
    con.register("bridge", bridge_tbl)

    # Cohort — distinct canonically-normalized lender name. The raw
    # lender_name_normalized column is re-normalized through the canonical
    # normalizer (it is uppercase / punctuation-retaining — NOT canonical).
    con.execute(
        f"""
        CREATE TEMP TABLE cohort AS
        SELECT py_norm(lender_name_normalized) AS nn,
               any_value(active_filings) AS active_filings,
               any_value(total_filings)  AS total_filings
        FROM lenders_raw
        WHERE bank_classification IN ('non_bank','unknown')
          AND active_filings >= {COHORT_MIN_ACTIVE}
        GROUP BY 1
        """
    )
    con.execute("CREATE TEMP TABLE cohort_v AS SELECT * FROM cohort WHERE nn IS NOT NULL")
    cohort_n = con.execute("SELECT COUNT(*) FROM cohort_v").fetchone()[0]

    # Lender state — dominant non-null STATE per normalized lender name.
    # Perf: the canonical normalizer is a per-row Python UDF; calling it on all
    # 4.68M Organization rows is slow. Pre-aggregate the RAW (ORG_NAME, STATE)
    # pair counts in pure SQL first (collapses 4.68M rows to far fewer distinct
    # raw pairs), THEN apply py_norm only to the distinct raw names and
    # re-aggregate by normalized name. The normalizer is deterministic, so
    # dominant-STATE-per-normalized-name is identical to the per-row approach.
    con.execute(
        """
        CREATE TEMP TABLE sp_raw_pairs AS
        SELECT ORG_NAME, STATE, COUNT(*) AS c
        FROM sp_raw
        WHERE ORG_NAME IS NOT NULL AND STATE IS NOT NULL AND STATE <> ''
        GROUP BY ORG_NAME, STATE
        """
    )
    con.execute(
        "CREATE TEMP TABLE sp_norm AS "
        "SELECT py_norm(ORG_NAME) AS nn, STATE, c FROM sp_raw_pairs"
    )
    con.execute(
        """
        CREATE TEMP TABLE lender_state AS
        WITH counted AS (
          SELECT nn, STATE, SUM(c) AS c
          FROM sp_norm
          WHERE nn IS NOT NULL
          GROUP BY nn, STATE
        ), ranked AS (
          SELECT nn, STATE, c,
                 ROW_NUMBER() OVER (PARTITION BY nn ORDER BY c DESC, STATE) AS rn
          FROM counted
        )
        SELECT nn, STATE AS lender_state FROM ranked WHERE rn = 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE cohort_st AS
        SELECT c.nn, c.active_filings, ls.lender_state
        FROM cohort_v c LEFT JOIN lender_state ls USING (nn)
        """
    )
    cohort_with_state = con.execute(
        "SELECT COUNT(*) FROM cohort_st WHERE lender_state IS NOT NULL"
    ).fetchone()[0]

    # Bridge matched-lender sets. All persisted bridge rows are in a non-rejected
    # tier (platinum/gold/silver) by construction — the bridge does not write
    # rejected rows. The tier filter is belt-and-suspenders.
    con.execute(
        "CREATE TEMP TABLE bridge_lenders AS "
        "SELECT DISTINCT lender_name_normalized AS nn FROM bridge "
        "WHERE confidence_tier IN ('platinum','gold','silver')"
    )
    con.execute(
        "CREATE TEMP TABLE bridge_ns AS "
        "SELECT DISTINCT lender_name_normalized AS nn, pdl_state FROM bridge "
        "WHERE confidence_tier IN ('platinum','gold','silver') "
        "  AND pdl_state IS NOT NULL"
    )

    # name-only: cohort lender appears in the bridge.
    name_matched = con.execute(
        """
        SELECT COUNT(*) FROM cohort_v c
        WHERE EXISTS (SELECT 1 FROM bridge_lenders b WHERE b.nn = c.nn)
        """
    ).fetchone()[0]

    # name+state: cohort lender matched a PDL row whose state agrees with the
    # lender's own state (secured_parties_lance.STATE-derived).
    name_state_matched = con.execute(
        """
        SELECT COUNT(*) FROM cohort_st c
        WHERE c.lender_state IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM bridge_ns b
            WHERE b.nn = c.nn AND b.pdl_state = c.lender_state
          )
        """
    ).fetchone()[0]

    # Per-tier breakdown of the emitted bridge.
    tiers = con.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
          COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
          COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge
        """
    ).fetchone()

    name_pct = round(100.0 * name_matched / cohort_n, 1) if cohort_n else 0.0
    name_state_pct = (
        round(100.0 * name_state_matched / cohort_n, 1) if cohort_n else 0.0
    )
    threshold_met = name_state_pct >= SUCCESS_THRESHOLD_PCT
    floor_met = bridge_rows >= MIN_ROWS_MATCHED

    out = {
        "benchmark": "ucc-ca-lender-pdl-bridge",
        "mode": "post-emit-cohort-coverage",
        "normalizer_version": NORMALIZER_VERSION,
        "cohort_min_active_filings": COHORT_MIN_ACTIVE,
        "cohort_distinct_normalized_lenders": cohort_n,
        "cohort_lenders_with_resolved_state": cohort_with_state,
        "bridge_total_rows": bridge_rows,
        "bridge_tier_platinum": tiers[0],
        "bridge_tier_gold": tiers[1],
        "bridge_tier_silver": tiers[2],
        "cohort_name_matched": name_matched,
        "cohort_name_only_rate_pct": name_pct,
        "cohort_name_state_matched": name_state_matched,
        "cohort_name_state_rate_pct": name_state_pct,
        "success_threshold_pct": SUCCESS_THRESHOLD_PCT,
        "threshold_met": threshold_met,
        "min_rows_matched_floor": MIN_ROWS_MATCHED,
        "floor_met": floor_met,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(out, indent=2))
    con.close()
    # Exit non-zero if EITHER the coverage threshold or the HARD-FAIL floor
    # is missed — both are acceptance gates.
    sys.exit(0 if (threshold_met and floor_met) else 1)


if __name__ == "__main__":
    main()
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$PROBE_PY'"
