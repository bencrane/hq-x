"""SEC DERA FSDS — fsds_pre_lance Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/fsds/release=*/pre.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_pre_lance

BTREE scalar index on adsh (accession number — FK to sub.adsh).
Composite key: (adsh, report, line). ~50M rows historical.

Modal sizing (c5 — DataFusion sort-spill at BTREE creation on ~50M rows):
    memory=32768 (32GB) per PR #464 precedent.
    timeout=7200 (2h).
    Entrypoint: apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_pre_lance
Local execution requires ≥32GB RAM.

Floor: 25,000,000 (733,135/q × 69q × 50%).

Invocation (via Modal 32GB — recommended):
    doppler run --project hq-all --config prd -- \\
      modal run apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_pre_lance

Invocation (local, requires 32GB+ RAM):
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_sec_dera_fsds_pre_lance_emit.py --apply
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Pin LANCE_BYPASS_SPILLING at module level for documentation + safety.
# lance_emit.py also sets this before create_scalar_index; this
# setdefault makes it visible in the script-level environment for review.
os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_emit import LanceEmitConfig, run_cli

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

CONFIG = LanceEmitConfig(
    dataset_slug="sec_dera_fsds_pre_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/fsds",
    parquet_file_pattern="pre.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_pre_lance",
    btree_column="adsh",
)


def main(argv: list[str] | None = None) -> None:
    sys.exit(run_cli(CONFIG, argv))


if __name__ == "__main__":
    main()
