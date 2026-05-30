#!/usr/bin/env python3
"""Re-emit GLEIF LEI records as a Lance dataset on R2 (Wave 2 sweep).

Wave 2 of the Lance sweep -- the universal legal-entity identity spine.
GLEIF (Global Legal Entity Identifier Foundation) publishes weekly
snapshots of every LEI-registered entity worldwide (~3.3M rows). The R2
layout is ``gleif/snapshot=YYYY-MM-DD/lei_records.parquet``.

Why this is high-leverage: LEIs are the canonical legal-entity ID across
cross-source matching (FDIC, SEC, FINRA, foreign-bank disclosures, sanctions
lists, SAM.gov entity registrations, CMS provider rosters, etc.). Pinning
every entity to its LEI when one exists is the substrate for the partner-
matching engine's "show me everything about this entity" lookups.

BTREE on ``lei`` -- the 20-character canonical LEI identifier.
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
    dataset_slug="gleif_lei_records_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="gleif",
    parquet_file_pattern="lei_records.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/"
        "gleif/lei_records_lance"
    ),
    btree_column="lei",
)


if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
