#!/usr/bin/env python3
"""ACRIS incremental refresh — Socrata `:updated_at` cursor advance.

Usage:
    doppler run -- python3 scripts/run_acris_incremental_refresh.py rp-master
    doppler run -- python3 scripts/run_acris_incremental_refresh.py all

Picks up the last successful incremental watermark per dataset from
ops.acris_ingest_runs (mig 146) and queries Socrata SODA with
`$where=:updated_at > '<watermark>'`. Pages with $limit=50000 + $offset.

If no prior incremental watermark exists, this loader falls back to
the publisher's `:updated_at` for the most recent successful BULK run —
i.e. it picks up where the bulk backfill left off. If even that is
missing, it bails (run the bulk backfill first).

Each row is upserted via the standard chunked_upsert path, so the
incremental refresh and bulk backfill share idempotency semantics.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make sibling module importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from acris_common import (
    ALL_DATASET_KEYS,
    DEFAULT_CHUNK_SIZE,
    DATASETS,
    LOOKUP_KEYS,
    DatasetConfig,
    build_persist_row,
    chunked_upsert,
    connect,
    get_last_watermark,
    get_table_columns,
    ingest_run,
    mark_run_completed,
    paginate_socrata_json,
    update_run_progress,
)

logger = logging.getLogger("acris-incremental")


def _format_watermark(dt: datetime) -> str:
    """Socrata `$where` clause format. UTC, no offset, millisecond precision."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def _bootstrap_watermark(dataset_id: str) -> datetime | None:
    """If this dataset has never had an incremental run, look at the most
    recent successful bulk run and use the time it completed as the cursor.
    Bulk runs ingest a snapshot at completion time, so any rows newer than
    that watermark are genuinely new.

    Returns None if there's no prior bulk run either — caller should bail.
    """
    conn = connect(direct=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT completed_at
                FROM ops.acris_ingest_runs
                WHERE dataset_id = %s
                  AND ingest_mode = 'bulk'
                  AND status = 'completed'
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (dataset_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def refresh_one(cfg: DatasetConfig, run_id: uuid.UUID) -> None:
    print(f"[{cfg.key}] incremental refresh, dataset={cfg.socrata_4x4}")

    watermark = get_last_watermark(cfg.socrata_4x4)
    if watermark is None:
        watermark = _bootstrap_watermark(cfg.socrata_4x4)
        if watermark is None:
            print(
                f"[{cfg.key}] no prior bulk OR incremental run found — "
                "run scripts/run_acris_full_backfill.py first."
            )
            return
        print(f"[{cfg.key}] bootstrapping watermark from prior bulk run: {watermark}")
    else:
        print(f"[{cfg.key}] resuming from prior watermark: {watermark}")

    where_clause = f":updated_at > '{_format_watermark(watermark)}'"

    with ingest_run(
        run_id=run_id,
        dataset_id=cfg.socrata_4x4,
        ingest_mode="incremental",
        source_url=f"https://data.cityofnewyork.us/resource/{cfg.socrata_4x4}.json",
        watermark_before=watermark,
    ) as (audit_conn, handle):
        loader_conn = connect(direct=False)
        try:
            cols = get_table_columns(loader_conn, cfg)
            total_loaded = 0
            total_skipped = 0
            n_seen = 0
            t0 = time.monotonic()
            new_max_updated: datetime | None = None
            chunk_size = DEFAULT_CHUNK_SIZE

            for page in paginate_socrata_json(
                cfg.socrata_4x4,
                where=where_clause,
                order=":updated_at",
            ):
                # Track max(:updated_at) across the run for watermark_after.
                for r in page:
                    ts = r.get(":updated_at")
                    if ts:
                        # Socrata returns :updated_at as ISO string.
                        try:
                            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            if new_max_updated is None or parsed > new_max_updated:
                                new_max_updated = parsed
                        except ValueError:
                            pass

                # Build persist rows from page; chunk into batches.
                buf: list[dict[str, Any]] = []
                for r in page:
                    # Drop SODA system columns from the persist payload (we
                    # already captured `:updated_at` for the watermark).
                    raw = {k: v for k, v in r.items() if not k.startswith(":")}
                    buf.append(build_persist_row(raw, cfg, table_columns=cols))
                    if len(buf) >= chunk_size:
                        loaded, skipped = chunked_upsert(loader_conn, cfg, buf)
                        total_loaded += loaded
                        total_skipped += skipped
                        buf = []
                if buf:
                    loaded, skipped = chunked_upsert(loader_conn, cfg, buf)
                    total_loaded += loaded
                    total_skipped += skipped
                n_seen += len(page)
                if (n_seen // 50000) and (n_seen // 50000) % 4 == 0:
                    print(
                        f"[{cfg.key}] seen={n_seen:,} loaded={total_loaded:,} "
                        f"skipped={total_skipped:,} max_updated={new_max_updated}"
                    )
                    update_run_progress(
                        handle,
                        rows_loaded=total_loaded,
                        rows_skipped_idempotent=total_skipped,
                    )

            elapsed = time.monotonic() - t0
            mark_run_completed(
                handle,
                rows_loaded=total_loaded,
                rows_skipped_idempotent=total_skipped,
                watermark_after=new_max_updated or watermark,
            )
            print(
                f"[{cfg.key}] DONE seen={n_seen:,} loaded={total_loaded:,} "
                f"skipped={total_skipped:,} elapsed={elapsed:.0f}s "
                f"watermark_after={new_max_updated or watermark}"
            )
        finally:
            loader_conn.close()


ALL_KEYS = list(DATASETS.keys()) + ["all", "lookup-codes"]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dataset", choices=ALL_KEYS)
    args = p.parse_args()

    run_id = uuid.uuid4()
    print(f"=== ACRIS incremental refresh run {run_id} dataset={args.dataset} ===")

    if args.dataset == "lookup-codes":
        for k in LOOKUP_KEYS:
            refresh_one(DATASETS[k], run_id)
        return 0
    if args.dataset == "all":
        for k in ALL_DATASET_KEYS:
            refresh_one(DATASETS[k], run_id)
        return 0

    refresh_one(DATASETS[args.dataset], run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
