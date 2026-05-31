"""SEC DERA FSDS — fsds_num_lance Lance emit (Pattern A thin wrapper).

Reads ZSTD Parquet at:
    r2://dex-raw-landing-zone/sec-dera/fsds/release=*/num.parquet
and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_num_lance

Primary BTREE scalar index on adsh (accession number — FK to sub.adsh).
Secondary BTREE scalar index on tag (XBRL tag name — for tag-grain scan).
Composite key: (adsh, tag, version, ddate, qtrs, uom, segments, coreg).
~200-350M rows historical — LARGEST Lance dataset to date.
The "Refinancing / BDC Target Matrix" substrate: every XBRL-tagged numeric
fact across all 10-K/10-Q filings (DebtInstrumentMaturityDate, loan amounts, etc.).

Modal sizing (c6 — CRITICAL):
    memory=65536 (64GB) — primary + secondary BTREE creation on ~200M rows.
    timeout=14400 (4h) — DataFusion sort-spill + Arrow read of 4-5GB Parquet shards.
    Entrypoint: apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_num_lance
Local execution is INFEASIBLE (>32GB RAM required).

Floor: 150,000,000 (5-7M/q × 69q × 50%).

Invocation (via Modal 64GB, --detach recommended):
    doppler run --project hq-all --config prd -- \\
      modal run --detach apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_num_lance
Monitor via: modal app logs data-engine-x-sec-dera-fsds
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Pin LANCE_BYPASS_SPILLING at module level for documentation + safety.
# lance_emit.py also sets this before create_scalar_index; this
# setdefault makes it visible in the script-level environment for review.
os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_emit import LanceEmitConfig, run_cli

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

CONFIG = LanceEmitConfig(
    dataset_slug="sec_dera_fsds_num_lance",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="sec-dera/fsds",
    parquet_file_pattern="num.parquet",
    partition_mode="multi_release",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_num_lance",
    btree_column="adsh",
)


def main(argv: list[str] | None = None) -> None:
    rc = run_cli(CONFIG, argv)
    if rc == 0:
        # Build secondary BTREE on tag (XBRL tag name — for BDC/refinancing tag-grain filters).
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
        ds.create_scalar_index("tag", index_type="BTREE", replace=True)
        logging.getLogger(__name__).info("Secondary BTREE on tag built.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
