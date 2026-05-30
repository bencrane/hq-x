#!/usr/bin/env python3
"""Re-emit GLEIF Level-2 relationship records as a Lance dataset on R2.

Cycle: ucc-gleif-identity-spine (s1).

Reads the GLEIF Level-2 relationship_records.parquet from the latest snapshot
at s3://dex-raw-landing-zone/gleif/snapshot=YYYY-MM-DD/relationship_records.parquet
and writes to polaris-warehouse/gleif/relationship_records_lance/.

Schema (verbatim from raw parquet — do NOT rename columns):
  relationship_id        string  (PK)
  start_node_lei         string  (child entity for IS_ULTIMATELY_CONSOLIDATED_BY)
  end_node_lei           string  (parent entity for IS_ULTIMATELY_CONSOLIDATED_BY)
  relationship_type      string
  relationship_status    string  (ACTIVE | INACTIVE | NULL)
  relationship_period_start  date32[day]
  relationship_period_end    date32[day]
  initial_registration_date  date32[day]
  last_update_date           date32[day]
  registration_status        string
  gleif_snapshot_date        date32[day]
  snapshot                   date32[day]

CRITICAL: columns are start_node_lei / end_node_lei, NOT parent_lei / child_lei.
For IS_ULTIMATELY_CONSOLIDATED_BY: start_node_lei IS the child, end_node_lei IS the parent.

647,268 total rows across all relationship types (validator-confirmed 2026-05-12).

Arrow-bridge pattern (NOT the lance-duckdb extension).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_gleif_relationships_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_gleif_relationships_lance.py --dry-run
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
    dataset_slug="gleif_relationship_records_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="gleif",
    parquet_file_pattern="relationship_records.parquet",
    partition_mode="latest_snapshot",
    lance_uri=(
        "s3://dex-raw-landing-zone/polaris-warehouse/"
        "gleif/relationship_records_lance"
    ),
    btree_column="relationship_id",
)

if __name__ == "__main__":
    raise SystemExit(run_cli(CONFIG))
