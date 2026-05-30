"""SEC Form ADV Part 1 — schedule_d_1f Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-adv/year=2024/table=schedule_d_1f/*.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/schedule_d_1f_lance

BTREE scalar index on crd_number (canonical firm-level PK per validator 2026-05-18).
Note: crd_number is non-unique here (11,835 distinct per 1,379,778 rows) — Lance
BTREE does not require uniqueness; it accelerates per-firm lookups.

Source row count: 1,379,778 (2024 snapshot).
Floor (99%): 1,365,980.

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      python3 scripts/run_sec_adv_schedule_d_1f_lance_emit.py --apply
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
    dataset_slug="sec_adv_schedule_d_1f_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-adv",
    parquet_file_pattern="table=schedule_d_1f/*.parquet",
    partition_mode="multi_year",
    multi_year_filter=[2024],
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/schedule_d_1f_lance",
    btree_column="crd_number",
)

if __name__ == "__main__":
    sys.exit(run_cli(CONFIG))
