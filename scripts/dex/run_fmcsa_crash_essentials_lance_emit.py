#!/usr/bin/env python3
"""Re-emit FMCSA crash_essentials as a Lance dataset on R2 (Wave 1 sweep).

Wave 1 of the Lance sweep — sibling cohort to the carrier_essentials canary.
Same R2 layout (snapshot=YYYY-MM-DD/data.parquet), same BTREE on dot_number,
same operational discipline. Pattern lives in
``apps/data-engine-x/scripts/_lib/lance_emit.py``.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with duckdb --with psycopg python3 \\
        apps/data-engine-x/scripts/run_fmcsa_crash_essentials_lance_emit.py \\
        --apply
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
    dataset_slug="fmcsa_crash_essentials_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="fmcsa-derived/crash_essentials",
    parquet_file_pattern="data.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/crash_essentials_lance"
    ),
    btree_column="dot_number",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
