#!/usr/bin/env python3
"""Re-emit USAspending contracts as a Lance dataset on R2 (Wave 1 sweep).

Wave 1 of the Lance sweep — federal contract awards. The R2 layout is
``usaspending/contracts/year=YYYY/data.parquet`` (one Parquet per fiscal
year). BTREE on ``recipient_uei`` — the dominant access pattern is
"show me everything awarded to UEI X" for partner-matching.

Per the directive: we re-emit only the actively-refreshed years
(2024, 2025, 2026) — the historical years 2008-2014 are stale snapshots
that need their own backfill cycle if/when needed.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_emit import LanceEmitConfig, run_cli  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


CONFIG = LanceEmitConfig(
    dataset_slug="usaspending_contracts_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="usaspending/contracts",
    parquet_file_pattern="data.parquet",
    partition_mode="multi_year",
    multi_year_filter=[2024, 2025, 2026],
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
    ),
    btree_column="recipient_uei",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
