#!/usr/bin/env python3
"""Re-emit NYC Recent Contract Awards as a Lance dataset on R2.

Source dataset (NYC Open Data Socrata): qyyg-4tf5 "Recent Contract Awards"
(~52K rows; mix of award notices and pre-award solicitations — downstream
bridges may filter to `type_of_notice_description='Award'`).
Ingested by `apps/data-engine-x/modal/ny_nyc_local_awards_ingest_app.py`.

R2 input layout:
    s3://dex-raw-landing-zone/nyc-contract-awards/snapshot=YYYY-MM-DD/data.parquet

Lance output:
    s3://dex-raw-landing-zone/polaris-warehouse/nyc/contract_awards_lance

Primary BTREE: contract_id (synthetic sha1[:16] of pin+agency+vendor+amount+start_date).
Secondary BTREE on vendor_name_normalized added post-emit via ds.create_scalar_index
(supports bridge JOINs and downstream cohort scans).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_nyc_contract_awards_lance_emit.py --apply
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
    dataset_slug="nyc_contract_awards_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="nyc-contract-awards",
    parquet_file_pattern="data.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/nyc/contract_awards_lance"
    ),
    btree_column="contract_id",
)


if __name__ == "__main__":
    import os

    exit_code = run_cli(CONFIG)
    if exit_code == 0:
        try:
            import lance

            storage_options = {
                "aws_endpoint": os.environ["R2_ENDPOINT"],
                "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
                "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
                "aws_region": "us-east-1",
                "aws_virtual_hosted_style_request": "false",
            }
            ds = lance.dataset(CONFIG.lance_uri, storage_options=storage_options)
            ds.create_scalar_index(
                "vendor_name_normalized", index_type="BTREE", replace=True
            )
            logging.getLogger(__name__).info(
                "secondary BTREE on vendor_name_normalized: OK"
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "secondary BTREE on vendor_name_normalized failed (non-fatal): %s", exc
            )

    raise SystemExit(exit_code)
