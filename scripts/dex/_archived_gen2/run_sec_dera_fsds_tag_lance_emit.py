"""SEC DERA FSDS — fsds_tag_lance Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/fsds/release=*/tag.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_tag_lance

BTREE scalar index on tag (XBRL tag name — identity spine for tag-grain joins).
Composite logical key: (tag, version). Heavy cross-quarter overlap (XBRL tags reused).
~5M rows historical (reference-table semantics). Standard 8GB Modal / local execution.

Floor: 2,500,000 (91,795/q × 69q × 50% — tag dictionary deduplication unknown).

Invocation:
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_sec_dera_fsds_tag_lance_emit.py --apply
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
    dataset_slug="sec_dera_fsds_tag_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/fsds",
    parquet_file_pattern="tag.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_tag_lance",
    btree_column="tag",
)

if __name__ == "__main__":
    sys.exit(run_cli(CONFIG))
