"""Refresh ops.data_source_catalog_r2_snapshot — runs locally or under Modal.

Per source row in ops.data_source_catalog (is_active = TRUE):
  1. R2 inventory probe — boto3 list_objects_v2 paginator over r2_prefix
     → r2_file_count, r2_total_bytes, r2_latest_modified
  2. RW source-count probe — count(*) FROM rw_catalog.rw_sources WHERE name LIKE 'source_<slug>%'
  3. RW essentials-MV probe (only if essentials_mv_name is set):
     → rw_essentials_exists from pg_class
     → rw_essentials_rows from count(*) on the MV (capped via statement_timeout)
  4. UPSERT into ops.data_source_catalog_r2_snapshot keyed on source_slug.

Idempotent: re-running with the same R2/RW state produces the same UPSERT
modulo cached_at. Wall-clock ≈ 3-5 min for ~39 sources (R2 inventory dominates).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any

import boto3
import psycopg2
from psycopg2.extras import execute_values


def _connect_pg() -> Any:
    """Direct connection — Modal injects DATABASE_URL; locally Doppler injects
    DEX_DB_URL_POOLED. Either is acceptable for read+UPSERT against ops.*"""
    url = (
        os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError("No DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT / DATABASE_URL set")
    return psycopg2.connect(url)


def _connect_rw() -> Any:
    return psycopg2.connect(
        host=os.environ["RISINGWAVE_HOST"],
        port=os.environ["RISINGWAVE_PORT"],
        user=os.environ["RISINGWAVE_USER"],
        password=os.environ["RISINGWAVE_PASSWORD"],
        database=os.environ["RISINGWAVE_DATABASE"],
    )


def _r2_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _probe_r2(s3: Any, r2_prefix: str) -> tuple[int, int, datetime | None]:
    paginator = s3.get_paginator("list_objects_v2")
    file_count = 0
    total_bytes = 0
    latest_modified: datetime | None = None
    for page in paginator.paginate(Bucket="dex-raw-landing-zone", Prefix=r2_prefix):
        for obj in page.get("Contents", []):
            file_count += 1
            total_bytes += obj["Size"]
            mod = obj["LastModified"]
            if latest_modified is None or mod > latest_modified:
                latest_modified = mod
    return file_count, total_bytes, latest_modified


def _probe_rw(rw_conn: Any, source_slug: str, essentials_mv_name: str | None) -> tuple[int, bool | None, int | None]:
    """Returns (rw_source_count, rw_essentials_exists, rw_essentials_rows).
    rw_essentials_exists is None if essentials_mv_name is NULL.

    Row count is sourced from rw_catalog.rw_table_stats.total_key_count via a
    pg_class.oid join — O(1) regardless of MV size. count(*) on streaming MVs
    can take 60+s on multi-million-row state tables, which previously NULLed
    rw_essentials_rows on every MV >~500K rows under the old 5s statement
    timeout. The stat is exact for projection MVs and overcounts intermediate
    aggregation state for aggregation-heavy MVs (e.g. mv_sam_usaspending_aggregated
    reports ~143K vs the 102K final rows — a ~40% overcount on a small base).
    Acceptable for order-of-magnitude operator visibility.
    """
    with rw_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM rw_catalog.rw_sources WHERE name LIKE %s",
            (f"source_{source_slug}%",),
        )
        rw_source_count = cur.fetchone()[0]

    rw_essentials_exists: bool | None = None
    rw_essentials_rows: int | None = None
    if essentials_mv_name:
        with rw_conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.total_key_count
                  FROM pg_class c
                  JOIN rw_catalog.rw_table_stats s ON s.id = c.oid
                 WHERE c.relname = %s
                """,
                (essentials_mv_name,),
            )
            row = cur.fetchone()
            if row is None:
                rw_essentials_exists = False
            else:
                rw_essentials_exists = True
                rw_essentials_rows = row[0]

    return rw_source_count, rw_essentials_exists, rw_essentials_rows


def main() -> int:
    pg = _connect_pg()
    rw = _connect_rw()
    s3 = _r2_client()

    with pg.cursor() as cur:
        cur.execute("""
            SELECT source_slug, r2_prefix, essentials_mv_name
              FROM ops.data_source_catalog
             WHERE is_active = TRUE
             ORDER BY source_slug
        """)
        sources = cur.fetchall()

    print(f"refresh_data_source_catalog: probing {len(sources)} sources", flush=True)

    rows_to_upsert: list[tuple[Any, ...]] = []
    for source_slug, r2_prefix, essentials_mv_name in sources:
        try:
            file_count, total_bytes, latest_modified = _probe_r2(s3, r2_prefix)
        except Exception as exc:
            print(f"  {source_slug}: R2 probe failed: {exc}", flush=True)
            file_count, total_bytes, latest_modified = 0, 0, None

        try:
            rw_source_count, rw_ess_exists, rw_ess_rows = _probe_rw(
                rw, source_slug, essentials_mv_name
            )
        except Exception as exc:
            print(f"  {source_slug}: RW probe failed: {exc}", flush=True)
            rw_source_count, rw_ess_exists, rw_ess_rows = 0, None, None

        rows_to_upsert.append((
            source_slug, file_count, total_bytes, latest_modified,
            rw_source_count, rw_ess_exists, rw_ess_rows,
            datetime.now(timezone.utc),
        ))
        print(
            f"  {source_slug}: r2_files={file_count} bytes={total_bytes} "
            f"rw_sources={rw_source_count} ess_exists={rw_ess_exists} ess_rows={rw_ess_rows}",
            flush=True,
        )

    with pg.cursor() as cur:
        execute_values(cur, """
            INSERT INTO ops.data_source_catalog_r2_snapshot (
              source_slug, r2_file_count, r2_total_bytes, r2_latest_modified,
              rw_source_count, rw_essentials_exists, rw_essentials_rows, cached_at
            ) VALUES %s
            ON CONFLICT (source_slug) DO UPDATE SET
              r2_file_count        = EXCLUDED.r2_file_count,
              r2_total_bytes       = EXCLUDED.r2_total_bytes,
              r2_latest_modified   = EXCLUDED.r2_latest_modified,
              rw_source_count      = EXCLUDED.rw_source_count,
              rw_essentials_exists = EXCLUDED.rw_essentials_exists,
              rw_essentials_rows   = EXCLUDED.rw_essentials_rows,
              cached_at            = EXCLUDED.cached_at
        """, rows_to_upsert)
        pg.commit()

    print(f"refresh_data_source_catalog: upserted {len(rows_to_upsert)} rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
