"""Validator baseline probe for Cycle B — contracts × rolled-up subawards Lance emit.

Cycle B = Pattern A enriched-cohort emit at PRIME-AWARD-KEY grain. Reads:
  - usaspending.contracts_lance          (transaction grain; ~15.5M rows per Cycle A predecessor probe)
  - usaspending.contract_subawards_lance (FSRS subaward grain; 16,879 rows per PR #471)

Output (PRE-RUN, future) goes to:
  - bridges/contracts_with_subawards_lance/  (1 row per prime award key)

CRITICAL FINDING (probe 2026-05-16):
  contracts_lance does NOT carry a column literally named `prime_award_unique_key`.
  The actual prime-award identifier in contracts_lance is `contract_award_unique_key`
  (string), same format as contract_subawards_lance.prime_award_unique_key
  (e.g. `CONT_AWD_xxx_xxx_xxx_xxx`, `CONT_IDV_xxx`). Verified by 97.08% intersection
  between contracts.contract_award_unique_key DISTINCT set and
  contract_subawards.prime_award_unique_key DISTINCT set.

  Resolution: executor MUST alias `contract_award_unique_key` ->
  `prime_award_unique_key` in the output schema. This is a USAspending naming-drift
  artifact (the official API names the column `contract_award_unique_key` in the
  prime contracts file but `prime_award_unique_key` in the subaward file — both
  refer to the same canonical award identifier).

What this probe does (READ-ONLY — NO R2 writes, NO DB writes, NO migrations):

  1. Opens BOTH read-only Lance datasets via PyLance.
  2. Captures column types for each (audit uses for projection lock + TRY_CAST set).
  3. Confirms `contract_award_unique_key` exists in contracts_lance (prime key
     under USAspending naming). If MISSING, this cycle is BLOCKED.
  4. Counts distinct contract_award_unique_key in contracts_lance and distinct
     prime_award_unique_key in subawards, and the intersection.
  5. Identifies trap VARCHARs (per L49) that will be in the projected output
     and need TRY_CAST at write-time — for both datasets. (Note:
     contract_subawards_lance already has typed columns for subaward dates and
     amounts — no TRY_CAST needed there; contracts_lance has ALL string columns
     so the prime-side TRY_CAST work is real.)
  6. Surfaces NAICS column reality: contract_subawards_lance has NO
     subaward-specific NAICS — only `prime_award_naics_code` (the PRIME's NAICS
     carried into each subaward row). Directive's `subaward_naics_array` column
     would collapse to a single-element array = the prime's NAICS for every prime.
     Audit / operator decides: drop the column, or rename to clarify it's the
     prime's NAICS carried-through (no extra info gained).
  7. Sets the validator floor: 0.9 × N_primes (distinct contract_award_unique_key
     in contracts_lance).

Run via Doppler:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --with pylance --with duckdb python \\
      scripts/migration-checks/hq-all-contracts-with-subawards-lance-emit-baseline-probe.py

Validator predictions (see contract.md):
  p1: Lance write succeeds at-or-above floor.
  p2: prime grain dedup correct (no duplicate prime_award_unique_key in output).
  p3: Polaris registration idempotent success.
  p4: Railway deploy SUCCESS + runtime probe passes.
  p5: ops.data_sources row count delta = +1.
  p6: LEFT JOIN preserves all primes (subaward fields NULL for primes without
      subaward reports — sparse enrichment).
"""
from __future__ import annotations

import os
import sys
from typing import Any

CONTRACTS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
)
CONTRACT_SUBAWARDS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance"
)

FLOOR_MULTIPLIER = 0.9

# Columns projected from contracts_lance into the prime side of the output.
# Names per actual contracts_lance schema (validator probe, 2026-05-16).
# `contract_award_unique_key` IS the prime award key (output gets alias rename
# to `prime_award_unique_key` to match the canonical subaward column name).
EXPECTED_CONTRACT_PROJECTION = [
    "contract_award_unique_key",                       # output: prime_award_unique_key
    "recipient_uei",                                   # output: prime_awardee_uei
    "recipient_name",                                  # output: prime_awardee_name
    "naics_code",                                      # output: prime_naics_code
    "naics_description",                               # output: prime_naics_description
    "period_of_performance_start_date",                # output: prime_period_of_performance_start_date
    "period_of_performance_current_end_date",          # output: prime_period_of_performance_current_end_date
    "total_dollars_obligated",                         # output: prime_total_dollars_obligated
    "action_date",                                     # output: prime_action_date_latest (after DISTINCT ON)
    "awarding_agency_name",                            # output: prime_awarding_agency_name
    "awarding_sub_agency_name",                        # output: prime_awarding_sub_agency_name
    "primary_place_of_performance_state_code",         # output: prime_place_of_performance_state_code
    "primary_place_of_performance_county_name",        # output: prime_place_of_performance_county_name
]

# Columns projected from contract_subawards_lance into the rollup side.
# Names per actual schema (validator probe, 2026-05-16).
# NOTE: prime_award_naics_code is the PRIME's NAICS carried-through — there is
# NO subaward-specific NAICS in FSRS data.
EXPECTED_SUBAWARD_PROJECTION = [
    "prime_award_unique_key",
    "subawardee_uei",
    "subawardee_name",
    "subaward_amount",                                       # type: double — no TRY_CAST needed
    "subaward_action_date",                                  # type: date32 — no TRY_CAST needed
    "prime_award_naics_code",                                # the PRIME's NAICS (no subaward-specific NAICS exists)
    "subaward_primary_place_of_performance_state_code",      # subaward's state — exists
]


def storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _duckdb_with_r2() -> Any:
    """Connect DuckDB + register R2 SECRET (validator note L7 — exercise both
    read paths so any endpoint/auth drift surfaces here, NOT during the Modal run).
    """
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _trap_varchar_subset(
    ds: Any, expected_projection: list[str]
) -> list[tuple[str, str]]:
    """Return (col, type) for projection columns whose type is string-like.

    These are the trap-VARCHARs the executor MUST wrap in TRY_CAST when using
    them for aggregation, comparison, or any computed column (L49). Default
    position: cast at write-time so downstream consumers don't have to.
    """
    schema_names = set(ds.schema.names)
    out: list[tuple[str, str]] = []
    for col in expected_projection:
        if col not in schema_names:
            continue
        t = str(ds.schema.field(col).type)
        if t in ("string", "large_string"):
            out.append((col, t))
    return out


# ---------------------------------------------------------------------------
# Probe 1: contracts_lance
# ---------------------------------------------------------------------------
def probe_contracts_lance() -> dict[str, Any]:
    import lance

    so = storage_options()
    ds = lance.dataset(CONTRACTS_LANCE_URI, storage_options=so)
    total = ds.count_rows()
    print(f"CONTRACTS_TOTAL_ROWS: {total:,}")
    print(f"CONTRACTS_COLUMN_COUNT: {len(ds.schema.names)}")

    # CRITICAL: confirm contract_award_unique_key (USAspending's actual prime key)
    # exists. literal prime_award_unique_key does NOT — see module docstring.
    has_prime_key = "contract_award_unique_key" in ds.schema.names
    print(f"CONTRACTS_HAS_CONTRACT_AWARD_UNIQUE_KEY: {has_prime_key}")
    if not has_prime_key:
        print("CONTRACTS_PRIME_KEY_CANDIDATES (for operator blocker):")
        for cand in (
            "prime_award_unique_key",
            "award_id_unique_key",
            "award_id_piid",
            "piid",
            "award_id_fain",
        ):
            present = cand in ds.schema.names
            print(f"  - {cand}: {'present' if present else 'absent'}")
        return {"has_prime_key": False, "total_rows": total}

    # Trap-VARCHAR scan over the directive's projection list.
    traps = _trap_varchar_subset(ds, EXPECTED_CONTRACT_PROJECTION)
    print(f"CONTRACTS_PROJECTION_TRAP_VARCHARS ({len(traps)}):")
    for col, t in traps:
        print(f"  - {col}: {t}")

    missing = [c for c in EXPECTED_CONTRACT_PROJECTION if c not in ds.schema.names]
    print(f"CONTRACTS_PROJECTION_MISSING_COLS ({len(missing)}):")
    for col in missing:
        print(f"  - {col}")

    # Distinct contract_award_unique_key — the floor source.
    print("scanning contracts_lance projected to (contract_award_unique_key) ...")
    scanner = ds.scanner(
        columns=["contract_award_unique_key"],
        filter=(
            "contract_award_unique_key IS NOT NULL "
            "AND contract_award_unique_key != ''"
        ),
    )
    arrow_tbl = scanner.to_table()
    print(f"CONTRACTS_NON_NULL_PRIME_KEY_ROWS: {arrow_tbl.num_rows:,}")

    import duckdb
    con = duckdb.connect()
    con.register("c", arrow_tbl)
    n_primes = con.execute(
        "SELECT COUNT(DISTINCT contract_award_unique_key) AS n FROM c"
    ).fetchone()[0]
    print(f"CONTRACTS_DISTINCT_CONTRACT_AWARD_UNIQUE_KEY: {n_primes:,}")

    # Materialize the prime set for the intersection probe.
    primes_set = set(arrow_tbl.column("contract_award_unique_key").to_pylist())
    primes_set.discard(None)
    primes_set.discard("")

    return {
        "has_prime_key": True,
        "total_rows": total,
        "column_count": len(ds.schema.names),
        "trap_varchars": traps,
        "missing_projection_cols": missing,
        "n_primes": n_primes,
        "non_null_prime_key_rows": arrow_tbl.num_rows,
        "primes_set": primes_set,
    }


# ---------------------------------------------------------------------------
# Probe 2: contract_subawards_lance
# ---------------------------------------------------------------------------
def probe_contract_subawards_lance(
    contracts_primes_set: set[str] | None,
) -> dict[str, Any]:
    import lance

    so = storage_options()
    ds = lance.dataset(CONTRACT_SUBAWARDS_LANCE_URI, storage_options=so)
    total = ds.count_rows()
    print(f"SUBAWARDS_TOTAL_ROWS: {total:,}")
    print(f"SUBAWARDS_COLUMN_COUNT: {len(ds.schema.names)}")

    # Required columns per directive `## Goal restated` subaward rollup.
    required_cols = [
        "prime_award_unique_key",
        "subawardee_uei",
        "subawardee_name",
        "subaward_amount",
        "subaward_action_date",
        "prime_award_naics_code",
        "subaward_primary_place_of_performance_state_code",
    ]
    missing_required = [c for c in required_cols if c not in ds.schema.names]
    print(f"SUBAWARDS_REQUIRED_MISSING ({len(missing_required)}):")
    for c in missing_required:
        print(f"  - {c}")

    # NAICS column reality probe — surface that there's no subaward-specific
    # NAICS; only `prime_award_naics_code` (the PRIME's NAICS carried through
    # to each subaward row).
    naics_cols = [
        c for c in ds.schema.names
        if "naics" in c.lower() or "industry" in c.lower() or c == "prime_award_naics_code"
    ]
    print(f"SUBAWARDS_NAICS_COLUMNS_PRESENT ({len(naics_cols)}):")
    for c in naics_cols:
        t = str(ds.schema.field(c).type)
        print(f"  - {c}: {t}")
    print(
        "WARN: FSRS does NOT capture a subaward-specific NAICS. Directive's "
        "`subaward_naics_array` would always collapse to a single-element array "
        "(= the prime's NAICS) for every prime — no actual rollup. Audit should "
        "either drop the column or rename to clarify."
    )

    # State columns — confirm subaward-specific state exists.
    state_cols = [
        c for c in ds.schema.names
        if "place_of_performance" in c.lower() and "state" in c.lower()
    ]
    print(f"SUBAWARDS_STATE_COLUMNS_PRESENT ({len(state_cols)}):")
    for c in state_cols:
        t = str(ds.schema.field(c).type)
        print(f"  - {c}: {t}")

    # Trap-VARCHAR scan over the projection list. contract_subawards_lance is
    # already cleanly typed for subaward dates/amounts (date32, double) per
    # Cycle A's TRY_CAST-at-write-time discipline. Expect minimal traps here.
    traps = _trap_varchar_subset(ds, EXPECTED_SUBAWARD_PROJECTION)
    print(f"SUBAWARDS_PROJECTION_TRAP_VARCHARS ({len(traps)}):")
    for col, t in traps:
        print(f"  - {col}: {t}")

    # Distinct prime_award_unique_key.
    if "prime_award_unique_key" not in ds.schema.names:
        print("SUBAWARDS_HAS_PRIME_KEY: False — BLOCKER")
        return {"has_prime_key": False, "total_rows": total}

    print("scanning contract_subawards_lance projected to (prime_award_unique_key) ...")
    scanner = ds.scanner(
        columns=["prime_award_unique_key"],
        filter="prime_award_unique_key IS NOT NULL AND prime_award_unique_key != ''",
    )
    arrow_tbl = scanner.to_table()
    print(f"SUBAWARDS_NON_NULL_PRIME_KEY_ROWS: {arrow_tbl.num_rows:,}")

    import duckdb
    con = duckdb.connect()
    con.register("s", arrow_tbl)
    n_primes_with_subs = con.execute(
        "SELECT COUNT(DISTINCT prime_award_unique_key) AS n FROM s"
    ).fetchone()[0]
    print(f"SUBAWARDS_DISTINCT_PRIME_AWARD_UNIQUE_KEY: {n_primes_with_subs:,}")

    intersection_size = 0
    intersection_fraction = 0.0
    if contracts_primes_set is not None:
        subaward_primes_set = set(arrow_tbl.column("prime_award_unique_key").to_pylist())
        subaward_primes_set.discard(None)
        subaward_primes_set.discard("")
        intersection = contracts_primes_set & subaward_primes_set
        intersection_size = len(intersection)
        if n_primes_with_subs > 0:
            intersection_fraction = intersection_size / n_primes_with_subs
        print(f"SUBAWARDS_X_CONTRACTS_INTERSECTION_SIZE: {intersection_size:,}")
        print(f"SUBAWARDS_X_CONTRACTS_INTERSECTION_FRACTION: {intersection_fraction:.4f}")
        if intersection_fraction < 0.95:
            print(
                "WARN: intersection fraction < 95% — FSRS subaward.prime_award_unique_key "
                "is expected to match the contracts.contract_award_unique_key set. "
                "Discrepancy may indicate (a) FSRS reports lag prime ingest, "
                "(b) primes have been deleted, or (c) FSRS references parent IDV "
                "awards. Surface to executor."
            )

    return {
        "has_prime_key": True,
        "total_rows": total,
        "column_count": len(ds.schema.names),
        "missing_required": missing_required,
        "naics_cols_present": naics_cols,
        "state_cols_present": state_cols,
        "trap_varchars": traps,
        "n_primes_with_subs": n_primes_with_subs,
        "intersection_size": intersection_size,
        "intersection_fraction": intersection_fraction,
    }


# ---------------------------------------------------------------------------
# Probe 3: exercise DuckDB-on-R2 (validator note L7 — both paths)
# ---------------------------------------------------------------------------
def probe_duckdb_r2_path() -> None:
    con = _duckdb_with_r2()
    secrets = con.execute(
        "SELECT name FROM duckdb_secrets() WHERE type='r2'"
    ).fetchall()
    print(f"DUCKDB_R2_SECRETS_REGISTERED: {len(secrets)}")
    for (name,) in secrets:
        print(f"  - {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(v):
            print(
                f"FATAL: {v} not set; run via "
                f"`doppler run --project hq-all --config prd -- ...`",
                file=sys.stderr,
            )
            return 2

    section("DuckDB-on-R2 SECRET registration (probe both read paths)")
    probe_duckdb_r2_path()

    section("contracts_lance (transaction grain — prime spine)")
    contracts_metrics = probe_contracts_lance()

    if not contracts_metrics.get("has_prime_key"):
        section("BLOCKED — contracts_lance has no contract_award_unique_key")
        return 3

    section("contract_subawards_lance (FSRS subaward grain)")
    subawards_metrics = probe_contract_subawards_lance(
        contracts_metrics.get("primes_set")
    )

    section("Volume floor recommendation")
    n_primes = contracts_metrics["n_primes"]
    floor = int(FLOOR_MULTIPLIER * n_primes)
    print(f"N_primes = CONTRACTS_DISTINCT_CONTRACT_AWARD_UNIQUE_KEY = {n_primes:,}")
    print(f"PROPOSED_MIN_ROW_FLOOR ({FLOOR_MULTIPLIER}× N_primes) = {floor:,}")

    section("Validator notes (paste into directive §Validator notes)")
    print("### Floor calibration (Cycle B)")
    print(f"  - N_primes (contracts.contract_award_unique_key distinct): {n_primes:,}")
    print(
        f"  - N_primes_with_subs (subawards.prime_award_unique_key distinct): "
        f"{subawards_metrics.get('n_primes_with_subs', 0):,}"
    )
    print(
        f"  - INTERSECTION: {subawards_metrics.get('intersection_size', 0):,} "
        f"(fraction of subaward primes joinable: "
        f"{subawards_metrics.get('intersection_fraction', 0):.4f})"
    )
    print(f"  - MIN_ROW_FLOOR ({FLOOR_MULTIPLIER}× N_primes): {floor:,}")
    print("### contracts_lance trap-VARCHARs in projection (TRY_CAST required)")
    for col, t in contracts_metrics.get("trap_varchars", []):
        print(f"  - {col}: {t}")
    print("### contract_subawards_lance trap-VARCHARs in projection")
    if not subawards_metrics.get("trap_varchars"):
        print("  (none — subaward_amount=double, subaward_action_date=date32 already typed)")
    for col, t in subawards_metrics.get("trap_varchars", []):
        print(f"  - {col}: {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
