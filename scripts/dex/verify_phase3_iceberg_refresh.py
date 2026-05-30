#!/usr/bin/env python3
"""Phase 3 verification: prove the refresh loop closes end-to-end.

What this exercises:
  1. Capture the current ``usaspending.contracts`` Iceberg snapshot id
     (``S_before``).
  2. Read the table pinned to ``S_before`` and confirm 0 rows match the
     synthetic-data marker filter (no synthetic data yet).
  3. Synthesize a tiny "new ingest" — 5 rows with a future-dated
     ``action_date`` and a recognizable ``recipient_uei`` marker —
     written as a fresh Parquet to a new R2 key
     ``usaspending/contracts/year=2026/snapshot=verify-phase3/data.parquet``,
     then registered into the Iceberg table via PyIceberg add_files.
  4. Capture the new snapshot id (``S_after``).
  5. Read the table at the live snapshot — confirm 5 synthetic rows
     appear.
  6. Read the table pinned to ``S_before`` — confirm 0 synthetic rows
     (time-travel works).
  7. (cleanup) Rollback to ``S_before`` + delete the synthetic R2 key.

Uses PyIceberg's scan API directly with row filters (NOT the full
evaluate pipeline) — this is the cheap-and-fast path that proves the
loop semantics. The full evaluate path is verified separately in Phase
2; reproving it here would re-scan all 15M rows for each of 3
evaluations.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.expressions import EqualTo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._lib.iceberg_catalog import get_catalog  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
SYNTH_KEY = "usaspending/contracts/year=2026/snapshot=verify-phase3/data.parquet"
ICEBERG_TABLE_ID = ("usaspending", "contracts")
SYNTH_MARKER_AGENCY = "PHASE3-VERIFY-AGENCY"
FUTURE_ACTION_DATE = "2026-05-15"
LARGE_OBLIGATION = "9999999999.00"
SYNTH_ROWS = 5
LOG = logging.getLogger("phase3-verify")


def _build_synthetic_parquet(out_path: Path, schema: pa.Schema) -> int:
    cols: dict[str, list] = {}
    for field in schema:
        cols[field.name] = [None] * SYNTH_ROWS
    cols["action_date"] = [FUTURE_ACTION_DATE] * SYNTH_ROWS
    cols["federal_action_obligation"] = [LARGE_OBLIGATION] * SYNTH_ROWS
    cols["recipient_uei"] = [f"VERIFY{i:06d}" for i in range(SYNTH_ROWS)]
    cols["recipient_name"] = [f"PHASE3_VERIFY_FIRM_{i}" for i in range(SYNTH_ROWS)]
    cols["award_id_piid"] = [f"VERIFY-PIID-{i}" for i in range(SYNTH_ROWS)]
    cols["awarding_agency_name"] = [SYNTH_MARKER_AGENCY] * SYNTH_ROWS
    table = pa.Table.from_pydict(cols, schema=schema)
    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return table.num_rows


def _make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _count_synth(table, *, snapshot_id: int | None = None) -> int:
    """Count synthetic rows in the table at the given snapshot.

    Uses PyIceberg's row filter (predicate pushdown) — scans only the
    synthetic Parquet, not all 15M rows.
    """
    scan_args = {"row_filter": EqualTo("awarding_agency_name", SYNTH_MARKER_AGENCY)}
    if snapshot_id is not None:
        scan_args["snapshot_id"] = snapshot_id
    return len(table.scan(**scan_args).to_arrow())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cleanup",
        action="store_true",
        help="After verification, delete the synthetic parquet + roll the "
        "table back to S_before",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    catalog = get_catalog()
    table = catalog.load_table(ICEBERG_TABLE_ID)
    snapshot_before = table.metadata.current_snapshot_id
    LOG.info("S_before snapshot id: %s", snapshot_before)

    pre_count = _count_synth(table)
    LOG.info("pre-refresh synthetic-marker count: %d  (expected: 0)", pre_count)
    assert pre_count == 0, f"pre-refresh expected 0, got {pre_count}"

    iceberg_schema = table.schema()
    arrow_schema = iceberg_schema.as_arrow()
    LOG.info("synthetic schema cols: %d", len(arrow_schema))

    with tempfile.NamedTemporaryFile(
        prefix="phase3_synth_", suffix=".parquet", delete=False
    ) as tf:
        synth_path = Path(tf.name)

    try:
        n = _build_synthetic_parquet(synth_path, arrow_schema)
        LOG.info(
            "wrote synthetic parquet: %s  rows=%d  size=%d",
            synth_path, n, synth_path.stat().st_size,
        )

        s3 = _make_s3_client()
        s3.upload_file(
            str(synth_path),
            R2_BUCKET,
            SYNTH_KEY,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        synth_uri = f"s3://{R2_BUCKET}/{SYNTH_KEY}"
        LOG.info("uploaded synthetic: %s", synth_uri)

        table.refresh()
        table.add_files([synth_uri])
        snapshot_after = table.metadata.current_snapshot_id
        LOG.info("S_after snapshot id: %s", snapshot_after)
        assert snapshot_after != snapshot_before

        post_count = _count_synth(table)
        LOG.info("post-refresh synthetic-marker count: %d  (expected: %d)",
                 post_count, SYNTH_ROWS)
        assert post_count == SYNTH_ROWS, (
            f"post-refresh expected {SYNTH_ROWS}, got {post_count}"
        )

        tt_count = _count_synth(table, snapshot_id=snapshot_before)
        LOG.info("time-travel(S_before) synthetic-marker count: %d  (expected: 0)",
                 tt_count)
        assert tt_count == 0, f"time-travel expected 0, got {tt_count}"

        LOG.info("PHASE 3 PASS")
        LOG.info("  S_before=%s  S_after=%s", snapshot_before, snapshot_after)
        LOG.info("  pre=0, post=%d, time-travel(S_before)=0", post_count)

        if args.cleanup:
            LOG.info("cleanup requested")
            try:
                table.manage_snapshots().rollback_to_snapshot(snapshot_before).commit()
                LOG.info("rolled iceberg table back to snapshot %s", snapshot_before)
            except Exception as e:
                LOG.warning("rollback failed: %s", e)
            try:
                s3.delete_object(Bucket=R2_BUCKET, Key=SYNTH_KEY)
                LOG.info("deleted synthetic R2 key %s", SYNTH_KEY)
            except Exception as e:
                LOG.warning("delete synthetic failed: %s", e)
        return 0
    finally:
        if synth_path.exists():
            synth_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
