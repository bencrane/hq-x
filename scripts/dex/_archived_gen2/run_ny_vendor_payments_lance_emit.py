#!/usr/bin/env python3
"""Re-emit NY Design+Construction Capital Project Vendor Payments as Lance on R2.

Source dataset (data.ny.gov Socrata): rb9h-9fit "Design & Construction
Capital Projects Vendor Payments: Beginning 2014" (~69K rows; NY construction
payments by State Contract number). Ingested by
``apps/data-engine-x/modal/ny_data_construction_ingest_app.py`` as a single
snapshot Parquet per cron run.

R2 input layout:
    s3://dex-raw-landing-zone/ny-data-construction/vendor_payments/snapshot=YYYY-MM-DD/data.parquet

Lance output:
    s3://dex-raw-landing-zone/polaris-warehouse/nystate/vendor_payments_lance

Primary BTREE: payment_id (synthetic sha1[:16] of contractnumber + vendor +
paymentamount + fiscalyear + quarter + typeofservice + county; computed at
ingest time in the Modal app).
Secondary BTREE on vendor_name_normalized added after run_cli via
ds.create_scalar_index.

Note (pivot 2026-05-18): the prior OpenBookNY OSC ColdFusion source is dead;
this wrapper now points at the data.ny.gov Socrata-sourced single-snapshot
layout.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ny_vendor_payments_lance_emit.py --apply
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
    dataset_slug="ny_vendor_payments_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="ny-data-construction/vendor_payments",
    parquet_file_pattern="data.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/nystate/vendor_payments_lance"
    ),
    btree_column="payment_id",
)


if __name__ == "__main__":
    import os

    exit_code = run_cli(CONFIG)
    if exit_code == 0:
        # Add secondary BTREE on vendor_name_normalized post-emit.
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
