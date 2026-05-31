"""SEC DERA Form D — related_persons Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/form-d/release=*/related_persons.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_related_persons_lance

BTREE scalar index on accessionnumber.
~3.9M rows historical — Modal memory=32768 (32GB) required per PR #464 precedent
(DataFusion sort-spill OOM at BTREE creation on multi-million-row datasets).
LANCE_BYPASS_SPILLING=true is set both at script level (for documentation) and
inside lance_emit.py (line 234) before create_scalar_index.
Floor: 2,500,000 (53,160 × 73q × 64%).

Invocation (local, requires 32GB+ RAM):
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_sec_dera_form_d_related_persons_lance_emit.py --apply

Invocation (via Modal 32GB container):
    doppler run --project hq-all --config prd -- \\
      modal run apps/data-engine-x/modal/sec_dera_form_d_app.py::emit_related_persons_lance
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Pin LANCE_BYPASS_SPILLING at module level for documentation + safety.
# lance_emit.py:234 also sets this before create_scalar_index; this
# setdefault makes it visible in the script-level environment for review.
os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.catalog_hooks import register_or_update_polaris
from scripts._lib.lance_emit import LanceEmitConfig, run_cli

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

CONFIG = LanceEmitConfig(
    dataset_slug="sec_dera_form_d_related_persons_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/form-d",
    parquet_file_pattern="related_persons.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_related_persons_lance",
    btree_column="accessionnumber",
)

if __name__ == "__main__":
    rc = run_cli(CONFIG)
    if rc == 0:
        # Polaris catalog lifecycle hook — fires after the BTREE on
        # accessionnumber is confirmed built inside run_cli. Idempotent;
        # raises on API failure.
        register_or_update_polaris(
            namespace="sec_dera",
            table_name="form_d_related_persons_lance",
            s3_uri=CONFIG.lance_uri,
            docstring=(
                "SEC DERA Form D related-persons natural-person spine "
                "(Pattern A direct-source hydration from ZSTD Parquet at "
                "sec-dera/form-d/release=*/related_persons.parquet; "
                "BTREE on accessionnumber)."
            ),
        )
    sys.exit(rc)
