#!/usr/bin/env python3
"""Re-emit NY State Authority Contracts as a Lance dataset on R2.

Source dataset (data.ny.gov Socrata): ehig-g5x3 "Procurement Report for State
Authorities" (~275K rows; 140K NY-vendor). Ingested by
``apps/data-engine-x/modal/ny_data_construction_ingest_app.py`` as a single
snapshot Parquet per cron run.

R2 input layout:
    s3://dex-raw-landing-zone/ny-data-construction/contracts/snapshot=YYYY-MM-DD/data.parquet

Lance output:
    s3://dex-raw-landing-zone/polaris-warehouse/nystate/contracts_lance

Primary BTREE: contract_id (synthetic sha1[:16] of authority_name + vendor_name
+ award_date + contract_amount; computed at ingest time in the Modal app).
Secondary BTREE on vendor_name_normalized added after run_cli via
ds.create_scalar_index (supports bridge JOINs and downstream cohort scans).

Note (pivot 2026-05-18): the prior OpenBookNY input layout
``ny-openbook/contracts/fiscal_year=YYYY/data.parquet`` is deprecated — the
OSC ColdFusion portal rejects all automated downloads. This wrapper now
points at the data.ny.gov Socrata-sourced single-snapshot layout.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ny_contracts_lance_emit.py --apply
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
    dataset_slug="ny_contracts_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="ny-data-construction/contracts",
    parquet_file_pattern="data.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/nystate/contracts_lance"
    ),
    btree_column="contract_id",
)


if __name__ == "__main__":
    import os

    exit_code = run_cli(CONFIG)
    if exit_code == 0:
        # Add secondary BTREE on vendor_name_normalized post-emit.
        # The JOIN in the bridge script uses vendor_name_normalized as the
        # RIGHT-side key; a BTREE here accelerates bridge-generation scans.
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
