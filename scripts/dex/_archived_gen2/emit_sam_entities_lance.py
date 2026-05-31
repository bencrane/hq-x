#!/usr/bin/env python3
"""Lance-emit: SAM monthly entity data.

New surface s6.5 — added by audit to unblock the SAM × SBA bridge rewrite
(s6). The validator confirmed SAM monthly entity data is NOT yet Lance-emitted
(the polaris-warehouse/sam_gov/ namespace only has opps_active_lance/).

Reads the latest `sam-gov/monthly/snapshot=<YYYY-MM-DD>/data.parquet` from R2
(hive-style snapshot=* directories). The `_lib/lance_emit.py::_detect_latest_snapshot`
helper globs on `snapshot=YYYY-MM-DD` which matches the hive-style layout.

Two snapshot directory shapes exist in R2:
  - sam-gov/monthly/YYYY-MM-DD/         (bare; legacy — not picked up by this script)
  - sam-gov/monthly/snapshot=YYYY-MM-DD/  (hive; latest as of 2026-05-03)

Writes to `polaris-warehouse/sam_gov/entities_lance/`.

Row floor: ≥ 800,000 (latest snapshot ~884K US-registered entities per validator).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_sam_entities_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_sam_entities_lance.py --dry-run
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_emit import LanceEmitConfig, run_cli  # noqa: E402

CONFIG = LanceEmitConfig(
    dataset_slug="sam_entities_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sam-gov/monthly",
    parquet_file_pattern="*.parquet",
    partition_mode="latest_snapshot",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance",
    btree_column="unique_entity_id",
    # unique_entity_id may be missing in some legacy rows — treat index as
    # optional so the write still succeeds if all rows are NULL.
    btree_optional=True,
)

if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
