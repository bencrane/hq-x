#!/usr/bin/env python3
"""Register CA UCC R2 parquet snapshots as Iceberg tables under namespace ``ucc_ca``.

Companion to ``init_iceberg_fmcsa_tables.py``. CA UCC R2 layout:

    s3://dex-raw-landing-zone/ucc/state=CA/stream=<stream>/snapshot=YYYY-MM-DD/data.parquet.zst

After this script runs, for each discovered stream:
  - Postgres ``iceberg_tables`` has a row for ``ucc_ca.<stream>``
  - R2 has metadata files under ``iceberg-warehouse/ucc_ca/<stream>/metadata/``
  - Every existing ``snapshot=*/data.parquet.zst`` file is registered as an
    Iceberg data file (no re-upload, no copy)
  - DuckDB can query the table via ``table.scan().to_duckdb(table_name=...)``

Stream → table-slug mapping (1:1, no slugify needed because streams are
already snake-case):
    "initial-dump"    → ucc_ca.initial_dump
    "weekly-delta"    → ucc_ca.weekly_delta
    "snapshot"        → ucc_ca.snapshot

The script is idempotent at the table level — re-running with the same
stream will refuse unless ``--allow-recreate`` is passed. ``add_files()``
itself is not idempotent (it appends file references), so re-registration
needs the table dropped first.

Usage:
    # Dry run — print discovered streams + parquets, no catalog writes:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_ucc_ca_tables.py --dry-run

    # Register one stream:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_ucc_ca_tables.py --stream initial-dump

    # Register every stream currently present in R2:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_ucc_ca_tables.py --all

DuckDB iceberg_scan() compatibility note carries over from the FMCSA
canary — query via PyIceberg's Arrow bridge (``table.scan().to_duckdb(...)``),
not via the iceberg extension. See ``scripts/_lib/iceberg_catalog.py``.

See directive ``~/Desktop/hq/directives/2026-05-12-hq-all-ucc-ca-ingest-scaffold.md``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict

import boto3
import pyarrow.parquet as pq
import s3fs
from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids
from pyiceberg.schema import assign_fresh_schema_ids

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._lib.iceberg_catalog import get_catalog  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
UCC_CA_PREFIX = "ucc/state=CA/"
NAMESPACE = ("ucc_ca",)
LOG = logging.getLogger("init-iceberg-ucc-ca")


def _stream_to_slug(stream: str) -> str:
    """Convert a stream name to a SQL-safe table slug.

    `initial-dump` → `initial_dump`, etc. Matches the FMCSA slugify intent
    but is simpler because UCC stream names are already snake-friendly.
    """
    return stream.replace("-", "_").lower()


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def discover_snapshot_files() -> dict[str, list[str]]:
    """Return ``{stream: [s3_uri, ...]}`` for every CA UCC parquet.

    Walks ``ucc/state=CA/`` and filters keys matching exactly:

        ucc/state=CA/stream={stream}/snapshot=YYYY-MM-DD/data.parquet.zst

    Skips any non-conforming keys (loose CSVs, log files, etc.).
    """
    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    by_stream: dict[str, list[str]] = defaultdict(list)
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=UCC_CA_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # ucc/state=CA/stream=<>/snapshot=<>/data.parquet.zst
            parts = key[len(UCC_CA_PREFIX):].split("/")
            if (
                len(parts) == 3
                and parts[0].startswith("stream=")
                and parts[1].startswith("snapshot=")
                and parts[2] in ("data.parquet", "data.parquet.zst")
            ):
                stream = parts[0][len("stream="):]
                by_stream[stream].append(f"s3://{R2_BUCKET}/{key}")
    for stream in by_stream:
        by_stream[stream].sort()
    return dict(by_stream)


def _read_arrow_schema(parquet_uri: str):
    fs = s3fs.S3FileSystem(
        client_kwargs={"endpoint_url": os.environ["R2_ENDPOINT"]},
        key=os.environ["R2_ACCESS_KEY_ID"],
        secret=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    return pq.read_schema(parquet_uri.replace("s3://", ""), filesystem=fs)


def register_stream(
    catalog,
    stream: str,
    parquet_uris: list[str],
    *,
    allow_recreate: bool,
) -> bool:
    """Create the Iceberg table for one stream and add_files() its snapshots.

    Returns True on success, False on a recoverable skip (table exists, no
    --allow-recreate). Hard errors raise.
    """
    if not parquet_uris:
        LOG.warning("stream %r has no parquets; skipping", stream)
        return False

    slug = _stream_to_slug(stream)
    table_id = (*NAMESPACE, slug)

    arrow_schema = _read_arrow_schema(parquet_uris[0])
    iceberg_schema = assign_fresh_schema_ids(
        _pyarrow_to_schema_without_ids(arrow_schema)
    )
    LOG.info(
        "  stream=%r → table=%s.%s — %d parquets, %d schema fields",
        stream,
        NAMESPACE[0],
        slug,
        len(parquet_uris),
        len(iceberg_schema.fields),
    )

    table_exists = False
    try:
        catalog.load_table(table_id)
        table_exists = True
    except Exception:
        table_exists = False

    if table_exists:
        if allow_recreate:
            LOG.warning(
                "    table %s already exists — dropping (--allow-recreate)",
                table_id,
            )
            catalog.drop_table(table_id)
        else:
            LOG.warning(
                "    table %s already exists; pass --allow-recreate to reregister",
                table_id,
            )
            return False

    table = catalog.create_table(table_id, schema=iceberg_schema)
    LOG.info("    created at %s", table.location())
    table.add_files(parquet_uris)
    LOG.info(
        "    add_files complete; snapshot=%s",
        table.metadata.current_snapshot_id,
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stream",
        action="append",
        default=[],
        help="Specific stream to register (repeatable). Mutually exclusive with --all. "
             "Examples: initial-dump, weekly-delta, snapshot.",
    )
    ap.add_argument(
        "--all", action="store_true",
        help="Register every stream discovered in R2.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print plan, no catalog writes.",
    )
    ap.add_argument(
        "--allow-recreate", action="store_true",
        help="Drop existing tables before re-registering.",
    )
    args = ap.parse_args()

    if not (args.stream or args.all or args.dry_run):
        ap.error("specify --stream, --all, or --dry-run")
    if args.stream and args.all:
        ap.error("--stream and --all are mutually exclusive")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    LOG.info(
        "discovering snapshot-pathed parquets under s3://%s/%s ...",
        R2_BUCKET, UCC_CA_PREFIX,
    )
    by_stream = discover_snapshot_files()
    LOG.info("discovered %d stream(s) with snapshot-pathed data:", len(by_stream))
    for stream in sorted(by_stream):
        LOG.info("  %-30s %d snapshots", stream, len(by_stream[stream]))

    if args.dry_run and not (args.stream or args.all):
        return 0

    if args.stream:
        unknown = [s for s in args.stream if s not in by_stream]
        if unknown:
            LOG.error("unknown stream(s): %s", unknown)
            LOG.error("known streams: %s", sorted(by_stream))
            return 2
        targets = {s: by_stream[s] for s in args.stream}
    else:
        targets = by_stream

    if args.dry_run:
        LOG.info("DRY RUN — would register %d stream(s):", len(targets))
        for stream in sorted(targets):
            LOG.info(
                "  ucc_ca.%s ← %d parquets",
                _stream_to_slug(stream), len(targets[stream]),
            )
        return 0

    catalog = get_catalog()
    catalog.create_namespace_if_not_exists(NAMESPACE)
    LOG.info("namespace %s ensured", NAMESPACE)

    succeeded: list[str] = []
    skipped: list[str] = []
    for stream in sorted(targets):
        try:
            if register_stream(
                catalog,
                stream,
                targets[stream],
                allow_recreate=args.allow_recreate,
            ):
                succeeded.append(stream)
            else:
                skipped.append(stream)
        except Exception as exc:
            LOG.error("FAILED to register %r: %s", stream, exc, exc_info=True)
            return 3

    LOG.info("DONE: %d registered, %d skipped", len(succeeded), len(skipped))
    if skipped:
        LOG.info("  skipped: %s", skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
