"""Validator baseline probe for the SAM entity POCs Lance emit volume floor.

Run via Doppler:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --with pylance python \\
      scripts/migration-checks/hq-all-sam-entity-pocs-lance-emit-baseline-probe.py

What it does:
  1. Loads the canonical `sam_gov.entities_lance` Lance dataset from R2 via
     the PyLance scanner (NO DuckDB Arrow-bridge for the read — the canonical
     pattern for Pattern A emits; matches the read shape in PR #464 / PR #467).
  2. Confirms via `lance.dataset(uri).schema.names` that all 6 POC last_name
     columns physically exist in the dataset under the EXACT spellings:
       - govt_bus_poc_last_name
       - alt_govt_bus_poc_last_name
       - past_perf_poc_poc_last_name        (note DOUBLE `poc`)
       - alt_past_perf_poc_last_name
       - elec_bus_poc_last_name
       - alt_elec_poc_bus_poc_last_name     (note NESTED `poc_bus_poc`)
     The 3rd and 6th POC kinds have non-obvious prefixes per the SAM extract
     layout (sam_gov_column_map.py rows 79-92 + 112-122; v2_positions 69-71
     and 102-104). Validator MUST confirm these exact names.
  3. For each POC kind, scans `sam_gov.entities_lance` projecting ONLY
     `unique_entity_id + <last_name_col>`, then counts rows where
     `last_name IS NOT NULL AND last_name != ''`. PyArrow compute over the
     materialized column.
  4. Sums per-kind counts → total expected POC rows in the long-form emit.
  5. Recommends `MIN_ROW_FLOOR = int(0.7 * total_expected)` per directive
     §"Volume floor calibration" (conservative for null-other-field filter —
     the executor's actual ALL-of-(first,middle,last,title)-null filter is
     stricter than just last_name non-null, so 0.7 lower-bounds the gap).

This probe is read-only: no R2 writes, no DB writes, no migrations.

Validator predictions (see contract.md):
  p1: Modal foreground-attach risk — executor MUST use `modal run --detach`
      per the FL-cycle lesson (Modal CLI disconnect kills attached jobs).
  p2: Non-obvious POC column naming — 3rd and 6th POC kinds have nested
      `poc_poc` / `poc_bus_poc` prefixes that an executor would easily
      mistype as `past_perf_poc_*` / `alt_elec_bus_poc_*`. This probe's
      schema-introspection step REJECTS such mistakes at validation time.
  p3: Floor miss if null density is higher than expected — primary POC
      kinds (govt_bus, elec_bus) are typically populated for nearly every
      registered entity; alternates (alt_*) are often null. If the actual
      population skews more alternate-null than predicted, total falls;
      0.7 multiplier provides cushion.
"""
from __future__ import annotations

import os
import sys

# Last-name field name per POC kind. Order matters (matches directive §Goal).
POC_KINDS: list[tuple[str, str]] = [
    ("govt_bus", "govt_bus_poc_last_name"),
    ("alt_govt_bus", "alt_govt_bus_poc_last_name"),
    ("past_perf", "past_perf_poc_poc_last_name"),  # DOUBLE poc
    ("alt_past_perf", "alt_past_perf_poc_last_name"),
    ("elec_bus", "elec_bus_poc_last_name"),
    ("alt_elec_bus", "alt_elec_poc_bus_poc_last_name"),  # NESTED poc_bus_poc
]

SAM_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)


def storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def assert_columns_exist(ds, kinds: list[tuple[str, str]]) -> None:
    """Fail loudly if any of the 6 POC last_name columns is missing.

    This is the validator's primary fragility check (p2): the 3rd and 6th
    POC kinds have non-obvious nested-`poc` prefixes that would crash the
    executor's projection at write-time, hours into a Modal run.
    """
    schema_names = set(ds.schema.names)
    print(f"SCHEMA_TOTAL_COLUMNS: {len(schema_names)}")
    missing = []
    for kind, col in kinds:
        present = col in schema_names
        marker = "OK" if present else "MISSING"
        print(f"  COLUMN_CHECK kind={kind:14s} col={col:34s} {marker}")
        if not present:
            missing.append((kind, col))
    if missing:
        print(
            "FATAL: the following POC last_name columns are NOT present in "
            f"{SAM_ENTITIES_LANCE_URI} — executor would crash at projection. "
            f"Missing: {missing}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def count_kind_nonblank_last_name(ds, kind: str, col: str) -> int:
    """Project (unique_entity_id, col) via PyLance scanner; count non-null,
    non-empty rows in pyarrow.compute. No DuckDB Arrow-bridge — direct PyLance
    pattern per validator §"Lance read pattern" constraint.
    """
    import pyarrow.compute as pc  # local import — Modal image not required here

    tbl = ds.scanner(
        columns=["unique_entity_id", col],
        # Lance filter expressions: SQL-ish; IS NOT NULL + != '' for non-blank.
        filter=f"{col} IS NOT NULL AND {col} != ''",
    ).to_table()
    n = tbl.num_rows
    # Belt-and-suspenders: PyArrow guard in case Lance filter quirks slip a
    # null through (Lance filter pushdown has historically had edge cases on
    # empty-string vs null parity).
    if n > 0:
        col_arr = tbl.column(col)
        mask = pc.and_(pc.is_valid(col_arr), pc.not_equal(col_arr, ""))
        n_strict = pc.sum(pc.cast(mask, "int64")).as_py() or 0
        if n_strict != n:
            print(
                f"  WARN: strict count {n_strict} differs from filter count {n} "
                f"for kind={kind} col={col}; using strict.",
                file=sys.stderr,
            )
            n = n_strict
    return n


def main() -> int:
    import lance  # noqa: E402  (import after env check)

    print("=== SAM entities Lance baseline probe ===")
    print(f"URI: {SAM_ENTITIES_LANCE_URI}")
    ds = lance.dataset(SAM_ENTITIES_LANCE_URI, storage_options=storage_options())
    total_entities = ds.count_rows()
    print(f"TOTAL_ENTITIES: {total_entities:,}")

    print("=== Schema introspection (POC last_name column existence) ===")
    assert_columns_exist(ds, POC_KINDS)

    print("=== Per-POC-kind non-blank last_name counts ===")
    per_kind: dict[str, int] = {}
    grand_total = 0
    for kind, col in POC_KINDS:
        n = count_kind_nonblank_last_name(ds, kind, col)
        per_kind[kind] = n
        grand_total += n
        pct = (n / total_entities * 100.0) if total_entities else 0.0
        print(f"  POC_KIND_COUNT kind={kind:14s} non_null_last_name={n:>10,}  ({pct:5.2f}% of entities)")

    print("=== Aggregate expectation ===")
    print(f"TOTAL_EXPECTED_POC_ROWS (sum of 6 kinds with non-null last_name): {grand_total:,}")
    proposed_floor = int(0.7 * grand_total)
    print(f"PROPOSED_MIN_ROW_FLOOR (0.7 * total_expected): {proposed_floor:,}")

    print("=== Validator notes (paste into directive §Validator notes) ===")
    print("### POC floor calibration")
    for kind, col in POC_KINDS:
        print(f"  - {kind:14s} ({col:34s}): {per_kind[kind]:>10,}")
    print(f"  - TOTAL_EXPECTED: {grand_total:,}")
    print(f"  - FLOOR (0.7×): {proposed_floor:,}")
    print(
        "  NOTE: the executor's ALL-of-(first, middle, last, title)-null filter "
        "is stricter than just last_name non-null — actual emit rows MAY be "
        "slightly lower than total_expected; floor is calibrated for this gap."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
