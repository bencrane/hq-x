#!/usr/bin/env python3
"""Re-emit SAM.gov opps active as a Lance dataset on R2 (Wave 1 sweep).

Wave 1 of the Lance sweep — daily SAM.gov opportunity feed. The R2 layout
is ``sam-gov-opps/active/snapshot=YYYY-MM-DD/<uuid>.parquet`` (one or more
UUID-named files per snapshot). BTREE on ``notice_id`` (the canonical
SAM.gov opportunity ID; how downstream partner-matching engines look up an
opp).

Per the directive: archived opps are out of scope here; they're a weekly
cadence and can join a later wave.
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
    dataset_slug="sam_gov_opps_active_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sam-gov-opps/active",
    parquet_file_pattern="*.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/opps_active_lance"
    ),
    btree_column="notice_id",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
