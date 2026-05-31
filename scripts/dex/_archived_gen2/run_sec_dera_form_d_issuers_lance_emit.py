"""SEC DERA Form D — issuers Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/form-d/release=*/issuers.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_issuers_lance

Primary BTREE scalar index on accessionnumber.
Secondary BTREE scalar index on cik (issuer-grain identity spine).
Composite key: (accessionnumber, issuer_seq_key).
Floor: 900,000 (15,982 × 73q × 70% — >1 issuer per filing).

Invocation:
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_sec_dera_form_d_issuers_lance_emit.py --apply
"""
from __future__ import annotations

import logging
import os
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
    dataset_slug="sec_dera_form_d_issuers_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/form-d",
    parquet_file_pattern="issuers.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_issuers_lance",
    btree_column="accessionnumber",
)

if __name__ == "__main__":
    rc = run_cli(CONFIG)
    if rc == 0:
        # Build secondary BTREE on cik (issuer-grain identity spine).
        import lance
        ds = lance.dataset(
            CONFIG.lance_uri,
            storage_options={
                "aws_endpoint": os.environ["R2_ENDPOINT"],
                "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
                "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
                "aws_region": "us-east-1",
                "aws_virtual_hosted_style_request": "false",
            },
        )
        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        ds.create_scalar_index("cik", index_type="BTREE", replace=True)
        logging.getLogger(__name__).info("Secondary BTREE on cik built.")
    sys.exit(rc)
