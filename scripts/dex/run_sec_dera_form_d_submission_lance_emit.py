"""SEC DERA Form D — submission Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/form-d/release=*/submission.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_submission_lance

BTREE scalar index on accessionnumber.
Floor: 800,000 (15,735 × 73q × 70%).

Invocation:
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_sec_dera_form_d_submission_lance_emit.py --apply
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
    dataset_slug="sec_dera_form_d_submission_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/form-d",
    parquet_file_pattern="submission.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_submission_lance",
    btree_column="accessionnumber",
)

if __name__ == "__main__":
    sys.exit(run_cli(CONFIG))
