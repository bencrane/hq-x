#!/usr/bin/env python3
"""One-shot script: create the ``usaspending.contracts`` Iceberg table and
register the 10 historical FY snapshot=YYYY-MM-DD/data.parquet files via
PyIceberg's ``add_files()`` API.

This is the canary cycle's Phase 1 deliverable. After this script runs:
  - Postgres has rows in ``iceberg_tables`` for ``usaspending.contracts``
  - R2 has metadata files at
    ``s3://dex-raw-landing-zone/iceberg-warehouse/usaspending/contracts/metadata/``
  - The 10 existing year={YYYY}/snapshot=2026-05-{06,08}/data.parquet files
    are registered as Iceberg data files (no re-upload)
  - DuckDB can read the table via PyIceberg's
    ``table.scan().to_duckdb(table_name=...)`` Arrow bridge

NOTE on DuckDB ``iceberg_scan()`` compatibility:
  PyIceberg ``add_files()`` writes absolute s3:// paths into the Iceberg
  manifest. DuckDB 1.5.x's iceberg extension has a bug where it tries to
  relativize the manifest's data-file paths against the table's location,
  even when they're already absolute. The workaround is to query via
  PyIceberg's Arrow-via-DuckDB bridge (``table.scan().to_duckdb(...)``),
  which is what ``audience_factory`` does. See the design note in
  ``scripts/_lib/iceberg_catalog.py``.

Run:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_canary_table.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import boto3
import pyarrow.parquet as pq
import s3fs
from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids
from pyiceberg.schema import assign_fresh_schema_ids

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._lib.iceberg_catalog import get_catalog  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
USASPENDING_PREFIX = "usaspending/contracts/"
NAMESPACE = ("usaspending",)
TABLE_NAME = "contracts"
LOG = logging.getLogger("init-iceberg-canary")


def discover_historical_parquets() -> list[str]:
    """Return s3:// URIs of every historical ``year=YYYY/snapshot=YYYY-MM-DD/data.parquet``.

    Skips the unkeyed ``year=YYYY/data.parquet`` duplicates that exist in
    R2 alongside the snapshot-pathed canonical files.

    Two historical writer scripts have written to this prefix:
      - ``run_usaspending_csv_to_r2_parquet.py`` (297-col, all-string)
        produced FY2024-2026 with snapshot=2026-05-06 (~32M rows total)
      - ``run_usaspending_backfill_r2_ingest.py`` (305-col, typed dates +
        normalized columns) produced FY2008-2014 with snapshot=2026-05-08
        (~23M rows total)

    The canary table uses the 297-col all-string schema, matching the
    writer being patched in Phase 1.4 (``run_usaspending_csv_to_r2_parquet.py``).
    Only FY2024-2026 files are registered. The 7 FY2008-2014 typed
    files will be registered when the schema sweep cycle decides whether
    to (a) coerce them down to the all-string schema or (b) widen the
    canary table to support both — that's a follow-up. They remain
    queryable as plain Parquet outside Iceberg today.
    """
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=USASPENDING_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Match year=YYYY/snapshot=YYYY-MM-DD/data.parquet exactly.
            parts = key[len(USASPENDING_PREFIX):].split("/")
            if (
                len(parts) == 3
                and parts[0].startswith("year=")
                and parts[1].startswith("snapshot=")
                and parts[2] == "data.parquet"
            ):
                # Filter to all-string 297-col schema (snapshot=2026-05-06
                # from run_usaspending_csv_to_r2_parquet.py — the writer
                # being patched in Phase 1.4). The 2026-05-08 historical
                # snapshots have a 305-col typed schema from a different
                # writer; they stay outside Iceberg for now.
                snapshot_date = parts[1].split("=", 1)[1]
                if snapshot_date == "2026-05-06":
                    keys.append(f"s3://{R2_BUCKET}/{key}")
    return sorted(keys)


def read_arrow_schema(parquet_uri: str) -> "pq.Schema":
    """Read a pyarrow schema from an s3-resident Parquet via s3fs."""
    fs = s3fs.S3FileSystem(
        client_kwargs={"endpoint_url": os.environ["R2_ENDPOINT"]},
        key=os.environ["R2_ACCESS_KEY_ID"],
        secret=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    return pq.read_schema(parquet_uri.replace("s3://", ""), filesystem=fs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print plan, don't write")
    ap.add_argument(
        "--allow-recreate",
        action="store_true",
        help="If the canary table already exists, drop + recreate it",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parquets = discover_historical_parquets()
    LOG.info("discovered %d historical FY snapshot parquets:", len(parquets))
    for p in parquets:
        LOG.info("  %s", p)
    if not parquets:
        LOG.error("no historical parquets found at s3://%s/%s", R2_BUCKET, USASPENDING_PREFIX)
        return 2

    schema_source = parquets[0]
    LOG.info("reading schema from %s", schema_source)
    arrow_schema = read_arrow_schema(schema_source)
    LOG.info("arrow schema columns: %d", len(arrow_schema))
    iceberg_schema = assign_fresh_schema_ids(_pyarrow_to_schema_without_ids(arrow_schema))
    LOG.info("iceberg schema fields: %d", len(iceberg_schema.fields))

    if args.dry_run:
        LOG.info("DRY RUN — no catalog writes")
        return 0

    catalog = get_catalog()
    catalog.create_namespace_if_not_exists(NAMESPACE)

    table_id = (*NAMESPACE, TABLE_NAME)
    table_exists = False
    try:
        catalog.load_table(table_id)
        table_exists = True
    except Exception:
        table_exists = False

    if table_exists:
        if args.allow_recreate:
            LOG.warning("table %s exists — dropping for recreate (--allow-recreate)", table_id)
            catalog.drop_table(table_id)
        else:
            LOG.error(
                "table %s already exists. Pass --allow-recreate to drop + reregister.",
                table_id,
            )
            return 3

    table = catalog.create_table(table_id, schema=iceberg_schema)
    LOG.info("created table %s at %s", table.name(), table.location())

    LOG.info("registering %d files via add_files()", len(parquets))
    table.add_files(parquets)
    LOG.info("add_files complete; snapshot=%s", table.metadata.current_snapshot_id)

    # Cheap rowcount via the PyIceberg→DuckDB bridge (counts manifest stats,
    # doesn't materialize all rows; ~seconds for 38M rows across 10 files).
    con = table.scan().to_duckdb(table_name=f"{NAMESPACE[0]}_{TABLE_NAME}")
    rc = con.execute(f"SELECT count(*) FROM {NAMESPACE[0]}_{TABLE_NAME}").fetchone()
    LOG.info("post-register rowcount: %s", f"{rc[0]:,}")
    LOG.info(
        "DONE: usaspending.contracts Iceberg table initialized, %d files, %s rows",
        len(parquets),
        f"{rc[0]:,}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
