#!/usr/bin/env python3
"""One-time backfill: bulk_ingest.feed_ingest_runs → ops.data_source_ingest_runs.

Idempotent: rows whose run_metadata->>'original_id' already exists in
ops.data_source_ingest_runs are skipped (not duplicated).

Column mapping (confirmed against 20260518000003_bulk_ingest_schema.sql,
schema unchanged after PR #341 per audit §"PR #341 coordination check"):

  bulk_ingest.feed_ingest_runs        →  ops.data_source_ingest_runs
  ─────────────────────────────────────────────────────────────────────
  id (uuid)                           →  run_metadata->>'original_id'
  source_id || ':' || feed_name       →  source_id (uuid, resolved via
                                          ops.data_sources.display_name match)
  started_at (or created_at if NULL)  →  started_at
  completed_at                        →  completed_at
  status (6-state)                    →  status (4-state enum, mapped below)
  rows_loaded                         →  rows_ingested
  bytes_downloaded (or payload_bytes) →  bytes_written
  NULL                                →  source_publish_at
  error_message                       →  error_message
  jsonb_build_object(...)             →  run_metadata

Status mapping (6-state → 4-state):
  pending | running  → running
  completed          → succeeded
  failed | timed_out → failed
  skipped            → skipped

Unmatched feed names (source_id||':'||feed_name not resolvable to a
display_name in ops.data_sources) get an auto-created data_sources row
with status='needs_triage', format='unknown', so no rows are silently
dropped — all are backfilled.

Usage:
  doppler run --project hq-all --config prd -- \\
    python3 apps/data-engine-x/scripts/migrate_feed_ingest_runs_to_observability_ledger.py
"""
from __future__ import annotations

import logging
import os
import sys
import uuid

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MIGRATED_FROM_KEY = "bulk_ingest.feed_ingest_runs"

STATUS_MAP = {
    "pending": "running",
    "running": "running",
    "completed": "succeeded",
    "failed": "failed",
    "timed_out": "failed",
    "skipped": "skipped",
}


def get_db_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT must be set via Doppler")
    return url


def load_source_map(conn: psycopg.Connection) -> dict[str, str]:
    """Build a mapping of display_name → source_id (uuid str)."""
    with conn.cursor() as cur:
        cur.execute("SELECT source_id, display_name FROM ops.data_sources")
        rows = cur.fetchall()
    return {row[1]: str(row[0]) for row in rows}


def resolve_source_id(
    conn: psycopg.Connection,
    bulk_source_id: str,
    feed_name: str,
    display_map: dict[str, str],
) -> str:
    """Resolve a bulk_ingest (source_id, feed_name) pair to an ops.data_sources uuid.

    Tries progressive fallback patterns. Creates a needs_triage row if nothing matches.
    """
    feed_lower = feed_name.lower().replace(" ", "_").replace("-", "_")
    src_lower = bulk_source_id.lower().replace("-", "_")

    candidates = [
        f"{src_lower}_{feed_lower}",                     # e.g. fmcsa_carrier
        f"fmcsa_derived_{feed_lower}",                   # e.g. fmcsa_derived_carrier
        f"iceberg_{src_lower}_{feed_lower}",             # e.g. iceberg_fmcsa_carrier
        f"{src_lower}",                                  # e.g. fmcsa (top-level)
    ]

    for candidate in candidates:
        if candidate in display_map:
            return display_map[candidate]

    # Auto-create a needs_triage row
    auto_display = f"bulk_ingest_unmapped_{src_lower}_{feed_lower}"
    if auto_display in display_map:
        return display_map[auto_display]

    result = conn.execute(
        """
        INSERT INTO ops.data_sources
            (display_name, storage_uri, format, owner_app, status)
        VALUES (%s, %s, 'unknown', 'data-engine-x', 'needs_triage')
        ON CONFLICT (display_name) DO UPDATE
            SET status = EXCLUDED.status
        RETURNING source_id
        """,
        (
            auto_display,
            f"bulk_ingest:{bulk_source_id}:{feed_name}",
        ),
    ).fetchone()
    assert result is not None
    new_id = str(result[0])
    display_map[auto_display] = new_id
    log.info("auto-created needs_triage source: %s", auto_display)
    return new_id


def migrate(conn: psycopg.Connection) -> None:
    display_map = load_source_map(conn)

    # Load all source rows not yet backfilled
    already_migrated = set(
        r[0]
        for r in conn.execute(
            """
            SELECT (run_metadata->>'original_id')::text
            FROM ops.data_source_ingest_runs
            WHERE run_metadata->>'migrated_from' = %s
            """,
            (MIGRATED_FROM_KEY,),
        ).fetchall()
    )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                id,
                source_id,
                feed_name,
                started_at,
                completed_at,
                status,
                rows_loaded,
                bytes_downloaded,
                payload_bytes,
                error_message,
                outcome,
                attempt,
                feed_date,
                evidence,
                created_at
            FROM bulk_ingest.feed_ingest_runs
            ORDER BY started_at ASC NULLS LAST
            """
        )
        source_rows = cur.fetchall()

    total_source = len(source_rows)
    skipped = 0
    migrated = 0
    unmapped = 0
    failed_rows: list[str] = []

    for row in source_rows:
        original_id = str(row["id"])
        if original_id in already_migrated:
            skipped += 1
            continue

        bulk_src = row["source_id"] or "unknown"
        feed = row["feed_name"] or "unknown"

        try:
            ops_source_id = resolve_source_id(conn, bulk_src, feed, display_map)
        except Exception as exc:
            log.warning("resolve failed for %s:%s — %s", bulk_src, feed, exc)
            unmapped += 1
            failed_rows.append(f"{bulk_src}:{feed}")
            continue

        raw_status = row["status"] or "skipped"
        mapped_status = STATUS_MAP.get(raw_status, "failed")

        started_at = row["started_at"] or row["created_at"]

        bytes_written = row["bytes_downloaded"] or row["payload_bytes"]

        run_metadata = {
            "migrated_from": MIGRATED_FROM_KEY,
            "original_id": original_id,
            "original_outcome": row["outcome"],
            "feed_name": feed,
            "attempt": row["attempt"],
            "feed_date": str(row["feed_date"]) if row["feed_date"] else None,
            "evidence": row["evidence"],
        }

        conn.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
                (source_id, started_at, completed_at, status, rows_ingested,
                 bytes_written, source_publish_at, error_message, run_metadata)
            VALUES (%s, %s, %s, %s::data_source_run_status, %s, %s, NULL, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                ops_source_id,
                started_at,
                row["completed_at"],
                mapped_status,
                row["rows_loaded"],
                bytes_written,
                row["error_message"],
                psycopg.types.json.Jsonb(run_metadata),
            ),
        )
        migrated += 1

        if migrated % 50 == 0:
            conn.commit()
            log.info("progress: %d/%d migrated", migrated, total_source - skipped)

    conn.commit()

    log.info(
        "backfill complete: source=%d migrated=%d skipped(already_done)=%d unmapped=%d",
        total_source, migrated, skipped, unmapped,
    )
    if failed_rows:
        log.warning("unmapped feed pairs: %s", failed_rows[:20])

    # Verify row count match
    new_count = conn.execute(
        "SELECT count(*) FROM ops.data_source_ingest_runs WHERE run_metadata->>'migrated_from' = %s",
        (MIGRATED_FROM_KEY,),
    ).fetchone()[0]

    if total_source > 0:
        pct = abs(total_source - new_count) / total_source
        if pct >= 0.01:
            log.error(
                "row count mismatch > 1%%: source=%d migrated=%d pct=%.2f%%",
                total_source, new_count, pct * 100,
            )
            sys.exit(1)
        else:
            log.info(
                "row count match OK: source=%d migrated_total=%d delta=%.2f%%",
                total_source, new_count, pct * 100,
            )


def main() -> int:
    db_url = get_db_url()
    log.info("connecting to DB (%s...)", db_url[:30])
    with psycopg.connect(db_url, autocommit=False) as conn:
        migrate(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
