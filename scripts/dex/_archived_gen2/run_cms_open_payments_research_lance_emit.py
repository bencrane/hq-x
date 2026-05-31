#!/usr/bin/env python3
"""Re-emit CMS Open Payments Research as a Lance dataset on R2 (Wave 2 sweep).

Wave 2 of the Lance sweep -- the "Research" payments feed from CMS Open
Payments (industry payments tied to clinical research). The R2 layout is
``cms-open-payments/year=YYYY/feed=research/<part-*.parquet | data.parquet>``.

Schema normalization: same as the General feed; 2024 onward uses the
15-column normalized schema with ``record_id`` as the primary key. Years
2018-2023 are pre-normalization and out of scope here.

BTREE on ``record_id``.
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
    dataset_slug="cms_open_payments_research_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="cms-open-payments",
    parquet_file_pattern="*.parquet",
    partition_mode="multi_year_feed",
    feed="research",
    multi_year_filter=[2024],
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/"
        "cms_open_payments/research_payments_lance"
    ),
    btree_column="record_id",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
