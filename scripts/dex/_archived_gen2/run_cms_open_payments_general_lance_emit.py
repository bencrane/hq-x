#!/usr/bin/env python3
"""Re-emit CMS Open Payments General as a Lance dataset on R2 (Wave 2 sweep).

Wave 2 of the Lance sweep -- the "General" payments feed from CMS Open
Payments (drug/biological/device payments to physicians and teaching
hospitals). The R2 layout is
``cms-open-payments/year=YYYY/feed=general/<part-*.parquet | data.parquet>``.

Schema normalization: 2024 onward uses a 15-column normalized schema with
``record_id`` as the primary key. Years 2018-2023 are pre-normalization
(~70+ raw columns) and are out of scope for this wave -- they stay on
Parquet+Iceberg until a separate backfill cycle. We filter to
``multi_year_filter=[2024]`` here; expand the filter list when new
normalized years arrive.

BTREE on ``record_id`` -- canonical CMS Open Payments transaction
identifier; the dominant access pattern is per-payment lookup for audit
trails and matching back to a manufacturer-payment dispute event.
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
    dataset_slug="cms_open_payments_general_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="cms-open-payments",
    parquet_file_pattern="*.parquet",
    partition_mode="multi_year_feed",
    feed="general",
    multi_year_filter=[2024],
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/"
        "cms_open_payments/general_payments_lance"
    ),
    btree_column="record_id",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
