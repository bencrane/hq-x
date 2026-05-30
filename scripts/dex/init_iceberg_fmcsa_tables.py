#!/usr/bin/env python3
"""Register FMCSA R2 parquet snapshots as Iceberg tables under namespace ``fmcsa``.

Companion to ``init_iceberg_canary_table.py`` (usaspending). FMCSA's R2 layout
mirrors usaspending's snapshot-pathed canonical: each feed lands as

    s3://dex-raw-landing-zone/fmcsa/{Feed Name}/snapshot=YYYY-MM-DD/data.parquet.zst

After this script runs, for each requested feed:
  - Postgres ``iceberg_tables`` has a row for ``fmcsa.<slug>``
  - R2 has metadata files under ``iceberg-warehouse/fmcsa/<slug>/metadata/``
  - Every existing ``snapshot=*/data.parquet.zst`` file is registered as an
    Iceberg data file (no re-upload, no copy)
  - DuckDB can query the table via ``table.scan().to_duckdb(table_name=...)``

The script is idempotent at the table level — re-running with the same feed
will refuse unless ``--allow-recreate`` is passed. ``add_files()`` itself is
not idempotent (it appends file references), so re-registration needs the
table dropped first.

Feed-name → table-slug mapping is in ``_slugify``. Examples:
    "Carrier"                          → fmcsa.carrier
    "Carrier - All With History"       → fmcsa.carrier_all_with_history
    "SMS AB Pass"                      → fmcsa.sms_ab_pass
    "Vehicle Inspections and Violations" → fmcsa.vehicle_inspections_and_violations
    "OUT OF SERVICE ORDERS"            → fmcsa.out_of_service_orders

The Parquet column schema is preserved verbatim from upstream — these are
the FMCSA-native column names like "USDOT Number", "Legal Name", etc.
(the R2 path bypasses the snake_case mapping that the postgres landing
zone applied). Audience specs filter on FMCSA-native column names.

Usage:
    # Dry run — print discovered feeds + parquets, no catalog writes:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_fmcsa_tables.py --dry-run

    # Register just one feed (the most-common entry point for piloting):
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_fmcsa_tables.py --feed Carrier

    # Register the 6 feeds needed for the GTM personas:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_fmcsa_tables.py \\
        --feed Carrier --feed AuthHist --feed Insurance \\
        --feed Revocation "--feed=Crash File" --feed Inspections\\ Per\\ Unit

    # Register every feed currently present in R2 (heavy — 31 tables):
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/init_iceberg_fmcsa_tables.py --all

DuckDB iceberg_scan() compatibility note carries over from canary — query
via PyIceberg's Arrow bridge (``table.scan().to_duckdb(...)``), not via the
iceberg extension. See ``scripts/_lib/iceberg_catalog.py``.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
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
FMCSA_PREFIX = "fmcsa/"
NAMESPACE = ("fmcsa",)
LOG = logging.getLogger("init-iceberg-fmcsa")


def _slugify(feed_name: str) -> str:
    """Convert a FMCSA feed_name to a SQL-safe Iceberg table name."""
    s = feed_name.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def discover_snapshot_files() -> dict[str, list[str]]:
    """Return ``{feed_name: [s3_uri, ...]}`` for every snapshot-pathed FMCSA parquet.

    Walks the entire ``fmcsa/`` prefix and filters to keys matching exactly:

        fmcsa/{Feed Name}/snapshot=YYYY-MM-DD/data.parquet.zst

    Skips the legacy ``fmcsa/{Feed}/{date}/{run_id}.parquet.zst`` layout and
    any other oddities (per-run shards, control files).
    """
    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    by_feed: dict[str, list[str]] = defaultdict(list)
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=FMCSA_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key[len(FMCSA_PREFIX):].split("/")
            if (
                len(parts) == 3
                and parts[1].startswith("snapshot=")
                and parts[2] in ("data.parquet", "data.parquet.zst")
            ):
                feed_name = parts[0]
                by_feed[feed_name].append(f"s3://{R2_BUCKET}/{key}")
    for feed in by_feed:
        by_feed[feed].sort()
    return dict(by_feed)


def _read_arrow_schema(parquet_uri: str):
    fs = s3fs.S3FileSystem(
        client_kwargs={"endpoint_url": os.environ["R2_ENDPOINT"]},
        key=os.environ["R2_ACCESS_KEY_ID"],
        secret=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    return pq.read_schema(parquet_uri.replace("s3://", ""), filesystem=fs)


def register_feed(
    catalog,
    feed_name: str,
    parquet_uris: list[str],
    *,
    allow_recreate: bool,
) -> bool:
    """Create the Iceberg table for one feed and add_files() its snapshots.

    Returns True on success, False on a recoverable skip (table exists, no
    --allow-recreate). Hard errors raise.
    """
    if not parquet_uris:
        LOG.warning("feed %r has no snapshot parquets; skipping", feed_name)
        return False

    slug = _slugify(feed_name)
    table_id = (*NAMESPACE, slug)

    arrow_schema = _read_arrow_schema(parquet_uris[0])
    iceberg_schema = assign_fresh_schema_ids(_pyarrow_to_schema_without_ids(arrow_schema))
    LOG.info(
        "  feed=%r → table=%s.%s — %d parquets, %d schema fields",
        feed_name,
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
            LOG.warning("    table %s already exists — dropping (--allow-recreate)", table_id)
            catalog.drop_table(table_id)
        else:
            LOG.warning("    table %s already exists; pass --allow-recreate to reregister", table_id)
            return False

    table = catalog.create_table(table_id, schema=iceberg_schema)
    LOG.info("    created at %s", table.location())
    table.add_files(parquet_uris)
    LOG.info("    add_files complete; snapshot=%s", table.metadata.current_snapshot_id)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--feed",
        action="append",
        default=[],
        help="Specific feed_name to register (repeatable). Mutually exclusive with --all.",
    )
    ap.add_argument("--all", action="store_true", help="Register every feed discovered in R2.")
    ap.add_argument("--dry-run", action="store_true", help="Print plan, no catalog writes.")
    ap.add_argument(
        "--allow-recreate",
        action="store_true",
        help="Drop existing tables before re-registering.",
    )
    args = ap.parse_args()

    if not (args.feed or args.all or args.dry_run):
        ap.error("specify --feed, --all, or --dry-run")
    if args.feed and args.all:
        ap.error("--feed and --all are mutually exclusive")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    LOG.info("discovering snapshot-pathed parquets under s3://%s/%s ...", R2_BUCKET, FMCSA_PREFIX)
    by_feed = discover_snapshot_files()
    LOG.info("discovered %d feeds with snapshot-pathed data:", len(by_feed))
    for feed in sorted(by_feed):
        LOG.info("  %-45s %d snapshots", feed, len(by_feed[feed]))

    if args.dry_run and not (args.feed or args.all):
        return 0

    if args.feed:
        unknown = [f for f in args.feed if f not in by_feed]
        if unknown:
            LOG.error("unknown feed(s): %s", unknown)
            LOG.error("known feeds: %s", sorted(by_feed))
            return 2
        targets = {f: by_feed[f] for f in args.feed}
    else:
        targets = by_feed

    if args.dry_run:
        LOG.info("DRY RUN — would register %d feeds:", len(targets))
        for feed in sorted(targets):
            LOG.info("  fmcsa.%s ← %d parquets", _slugify(feed), len(targets[feed]))
        return 0

    catalog = get_catalog()
    catalog.create_namespace_if_not_exists(NAMESPACE)
    LOG.info("namespace %s ensured", NAMESPACE)

    succeeded: list[str] = []
    skipped: list[str] = []
    for feed in sorted(targets):
        try:
            if register_feed(
                catalog,
                feed,
                targets[feed],
                allow_recreate=args.allow_recreate,
            ):
                succeeded.append(feed)
            else:
                skipped.append(feed)
        except Exception as exc:
            LOG.error("FAILED to register %r: %s", feed, exc, exc_info=True)
            return 3

    LOG.info("DONE: %d registered, %d skipped", len(succeeded), len(skipped))
    if skipped:
        LOG.info("  skipped: %s", skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
