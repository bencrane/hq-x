"""SEC Form ADV Part 1 — advw_main Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-adv/year=2024/table=advw_main/*.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/advw_main_lance

BTREE scalar index on crd_number (canonical firm-level PK per validator 2026-05-18).

Source row count: 21,076 (2024 snapshot).
Floor (99%): 20,865.

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      python3 scripts/run_sec_adv_advw_main_lance_emit.py --apply
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_emit import LanceEmitConfig, run_cli

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

CONFIG = LanceEmitConfig(
    dataset_slug="sec_adv_advw_main_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-adv",
    parquet_file_pattern="table=advw_main/*.parquet",
    partition_mode="multi_year",
    multi_year_filter=[2024],
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/advw_main_lance",
    btree_column="crd_number",
)

if __name__ == "__main__":
    sys.exit(run_cli(CONFIG))
