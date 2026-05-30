#!/usr/bin/env python3
"""Re-emit FMCSA authhist_essentials as a Lance dataset on R2 (Wave 1 sweep).

Wave 1 of the Lance sweep — authority-history cohort. Same shape as
carrier/crash: ``snapshot=YYYY-MM-DD/data.parquet`` under
``fmcsa-derived/authhist_essentials/``. BTREE on dot_number for per-DOT
authority-history lookups (the dominant access pattern).
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
    dataset_slug="fmcsa_authhist_essentials_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="fmcsa-derived/authhist_essentials",
    parquet_file_pattern="data.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/authhist_essentials_lance"
    ),
    btree_column="dot_number",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
