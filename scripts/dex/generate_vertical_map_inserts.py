#!/usr/bin/env python3
"""
Generate the INSERT VALUES statements for migration 104 from the proposed
verticals JSON and the LLM classification JSON.

Inputs:
  /tmp/proposed_verticals.json
  /tmp/naics_vertical_classifications_reviewed.json

Outputs (stdout):
  -- staffing_verticals VALUES rows
  ...
  -- naics_vertical_map VALUES rows
  ...
"""
from __future__ import annotations

import json
import sys

VERTICALS_PATH = "/tmp/proposed_verticals.json"
MAPPINGS_PATH = "/tmp/naics_vertical_classifications_reviewed.json"
MODEL_USED = "claude-opus-4-7"


def sql_str(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    with open(VERTICALS_PATH) as f:
        verticals = json.load(f)
    with open(MAPPINGS_PATH) as f:
        mappings = json.load(f)

    valid_keys = {v["vertical_key"] for v in verticals}

    print("-- staffing_verticals VALUES")
    rows = []
    for v in sorted(verticals, key=lambda x: x["sort_order"]):
        rows.append(
            f"({sql_str(v['vertical_key'])}, {sql_str(v['label'])}, "
            f"{sql_str(v['description'])}, {v['sort_order']})"
        )
    print(",\n".join(rows) + ";")

    print()
    print("-- naics_vertical_map VALUES")
    rows = []
    for m in mappings:
        vk = m["vertical_key"]
        if vk not in valid_keys:
            print(f"-- WARNING: unknown vertical_key {vk!r} for naics {m['naics_code']!r}; coerced to 'other'", file=sys.stderr)
            vk = "other"
        conf = m.get("confidence", "medium")
        if conf not in ("high", "medium", "review"):
            conf = "medium"
        rationale = m.get("rationale") or ""
        rows.append(
            f"({sql_str(m['naics_code'])}, {sql_str(vk)}, {sql_str(conf)}, "
            f"{sql_str(rationale)}, {sql_str(MODEL_USED)})"
        )
    print(",\n".join(rows) + ";")
    return 0


if __name__ == "__main__":
    sys.exit(main())
