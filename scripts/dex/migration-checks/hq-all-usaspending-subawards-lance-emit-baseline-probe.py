"""Validator baseline probe for USAspending subawards Lance-emit cycle.

Run via Doppler (NOTE: `--with pytz` is required — DuckDB needs pytz to
materialize timezone-aware timestamp columns in the sample rows; the trap-
field detection itself runs on the DESCRIBE output, which doesn't need it,
but the LIMIT 3 sample SELECT does):

    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --with pytz python \\
      scripts/migration-checks/hq-all-usaspending-subawards-lance-emit-baseline-probe.py

What it does (read-only):
  1. Opens both R2 parquets via DuckDB-on-R2 (httpfs + R2 SECRET, matching the
     canonical `_connect_duckdb()` pattern in
     `scripts/run_sam_entity_pocs_lance_emit.py::_connect_duckdb`).
  2. For each dataset, captures:
       - row count
       - column list with types
       - 3-row sample (truncated cells)
       - list of VARCHAR-typed columns matching L49 trap-field patterns
         (`*_amount`, `*_date`, `*_obligated`, `*_obligation`) — these are
         the TRY_CAST targets for the executor.
       - presence/absence of each of the 4 BTREE-target columns:
           prime_award_unique_key
           subaward_number
           subawardee_uei
           prime_awardee_uei
         If absent, the script searches for a likely synonym (audit will
         confirm the substitution before the executor writes Modal code).
  3. Computes audit-conservative volume floors as `floor(0.9 * row_count)`
     for each dataset. These get stamped back into the directive's
     `## Volume floors` table.

The probe writes to STDOUT only (no R2 writes, no DB writes, no migrations).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


CONTRACT_PARQUET = (
    "r2://dex-raw-landing-zone/usaspending/contract_subawards/"
    "year=2026/data.parquet"
)
ASSISTANCE_PARQUET = (
    "r2://dex-raw-landing-zone/usaspending/assistance_subawards/"
    "year=2026/data.parquet"
)

BTREE_TARGETS = [
    "prime_award_unique_key",
    "subaward_number",
    "subawardee_uei",
    "prime_awardee_uei",
]

# L49 trap-field patterns: VARCHAR columns whose names suggest numeric/date
# semantics. The executor MUST wrap these in TRY_CAST if downstream consumers
# read them as numeric/date OR if any computed column derives from them.
TRAP_PATTERNS = ["_amount", "_date", "_obligated", "_obligation"]


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _connect_duckdb():
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


def _probe_dataset(con, label: str, uri: str) -> dict[str, Any]:
    print(f"\n=== {label} ===", flush=True)
    print(f"URI: {uri}", flush=True)

    # row count
    row_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{uri}')"
    ).fetchone()[0]
    print(f"row_count: {row_count:,}", flush=True)

    # column list with types — DESCRIBE returns (column_name, column_type, null,
    # key, default, extra)
    desc_rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{uri}')"
    ).fetchall()
    columns = [(r[0], r[1]) for r in desc_rows]
    print(f"column_count: {len(columns)}", flush=True)

    # 3-row sample, truncate each cell repr to 60 chars
    sample_rows = con.execute(
        f"SELECT * FROM read_parquet('{uri}') LIMIT 3"
    ).fetchall()
    sample_cols = [r[0] for r in desc_rows]
    print("sample (first 3 rows):", flush=True)
    for i, row in enumerate(sample_rows):
        print(f"  row {i}:", flush=True)
        for col_name, cell in zip(sample_cols, row):
            cell_repr = repr(cell)
            if len(cell_repr) > 60:
                cell_repr = cell_repr[:57] + "..."
            print(f"    {col_name}: {cell_repr}", flush=True)

    # trap-field VARCHARs
    trap_varchars = []
    for col_name, col_type in columns:
        col_type_lower = col_type.lower()
        if "varchar" in col_type_lower or col_type_lower == "string":
            for pat in TRAP_PATTERNS:
                if pat in col_name.lower():
                    trap_varchars.append((col_name, col_type))
                    break
    print(f"\nL49 trap-field VARCHARs ({len(trap_varchars)}):", flush=True)
    for col_name, col_type in trap_varchars:
        print(f"  {col_name}: {col_type}", flush=True)

    # BTREE-target column presence
    col_name_set = {c[0].lower() for c in columns}
    btree_status = {}
    for target in BTREE_TARGETS:
        present = target.lower() in col_name_set
        if present:
            btree_status[target] = {"present": True, "synonym": None}
        else:
            # Heuristic synonym search — find columns whose name shares
            # ≥2 tokens with the target.
            target_tokens = set(target.split("_"))
            candidates = []
            for c, ctype in columns:
                c_tokens = set(c.lower().split("_"))
                shared = target_tokens & c_tokens
                if len(shared) >= 2:
                    candidates.append((c, ctype, sorted(shared)))
            btree_status[target] = {
                "present": False,
                "synonym_candidates": candidates,
            }
    print(f"\nBTREE-target column status:", flush=True)
    for target, status in btree_status.items():
        if status["present"]:
            print(f"  {target}: PRESENT", flush=True)
        else:
            cands = status["synonym_candidates"]
            if cands:
                print(f"  {target}: MISSING — synonym candidates:", flush=True)
                for c, ctype, shared in cands:
                    print(
                        f"    {c}: {ctype} (shared tokens: {shared})",
                        flush=True,
                    )
            else:
                print(f"  {target}: MISSING — NO synonym candidates", flush=True)

    # audit-conservative floor
    floor = int(row_count * 0.9)
    print(f"\nfloor (0.9 × row_count): {floor:,}", flush=True)

    return {
        "label": label,
        "uri": uri,
        "row_count": row_count,
        "column_count": len(columns),
        "columns": columns,
        "trap_varchars": trap_varchars,
        "btree_status": btree_status,
        "min_row_floor": floor,
    }


def main() -> int:
    con = _connect_duckdb()
    contract = _probe_dataset(con, "contract_subawards", CONTRACT_PARQUET)
    assistance = _probe_dataset(con, "assistance_subawards", ASSISTANCE_PARQUET)

    # JSON summary for downstream consumers (validator notes, audit).
    summary = {
        "contract_subawards": {
            "row_count": contract["row_count"],
            "column_count": contract["column_count"],
            "trap_varchar_count": len(contract["trap_varchars"]),
            "btree_present": [
                t for t, s in contract["btree_status"].items() if s["present"]
            ],
            "btree_missing": [
                t for t, s in contract["btree_status"].items() if not s["present"]
            ],
            "min_row_floor": contract["min_row_floor"],
        },
        "assistance_subawards": {
            "row_count": assistance["row_count"],
            "column_count": assistance["column_count"],
            "trap_varchar_count": len(assistance["trap_varchars"]),
            "btree_present": [
                t for t, s in assistance["btree_status"].items() if s["present"]
            ],
            "btree_missing": [
                t for t, s in assistance["btree_status"].items()
                if not s["present"]
            ],
            "min_row_floor": assistance["min_row_floor"],
        },
    }
    print("\n=== JSON SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
