"""Ingest-ledger unification helper.

Cycle: usaspending-pipeline-remediation (2026-05-13).

Problem:
    bulk_ingest.feed_ingest_runs carries USAspending (and SBA, FDIC, etc)
    failure rows with source_id as TEXT (e.g. 'usaspending_api_daily'). The
    canonical Phase 0c alert ledger reads from ops.data_source_ingest_runs
    where source_id is UUID (FK to ops.data_sources). Real bulk_ingest
    failures don't surface to Phase 0c alerts because the two ledgers are
    disjoint.

Solution (chosen by audit over a Postgres TRIGGER):
    Daily reconciliation cron — scans new bulk_ingest rows since last
    watermark, resolves source_id via display_name lookup, INSERTs mirror
    rows into ops.data_source_ingest_runs. Skips already-mirrored rows via
    idempotency-key in run_metadata.

Why cron over TRIGGER:
    1. Type mismatch (text → uuid) requires lookup; a trigger would either
       have to fail-silent or block the upstream INSERT — both bad.
    2. The two ledgers serve different audiences (bulk_ingest = harness
       runtime detail; ops = operator-facing). Mirroring everything in
       real time would clutter ops with retry/attempt noise.
    3. Daily cadence matches the cycle of cron-based ingests that write to
       bulk_ingest in the first place.

Mapping table (extend as new sources land):
    'usaspending_api_daily'  → display_name='usaspending_api_daily'
    'usaspending_daily'      → display_name='usaspending_contracts_lance'

Usage (called by modal/usaspending_daily_verify_app.py post-verify, or by
direct operator invocation):
    from scripts._lib.ingest_ledger_unify import reconcile_bulk_ingest_to_ops

    reconcile_bulk_ingest_to_ops(
        bulk_source_id='usaspending_api_daily',
        ops_display_name='usaspending_api_daily',
        since='1 day',
    )
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)


# Map bulk_ingest text source_id → ops.data_sources display_name.
# Extend as new cron-based ingests join the reconciliation set.
USASPENDING_LEDGER_MAP: dict[str, str] = {
    "usaspending_api_daily": "usaspending_api_daily",
    "usaspending_daily": "usaspending_contracts_lance",
}


def _resolve_ops_source_id(conn: psycopg.Connection, display_name: str) -> UUID | None:
    row = conn.execute(
        "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
        (display_name,),
    ).fetchone()
    return row[0] if row else None


def _bulk_status_to_ops_status(bulk_status: str, outcome: str | None) -> str:
    """Translate bulk_ingest.status + outcome → ops.data_source_run_status.

    bulk_ingest.status values seen in the wild: 'running', 'succeeded', 'failed'.
    ops.data_source_run_status enum: 'running', 'succeeded', 'failed', 'skipped'.
    """
    if bulk_status in {"running", "succeeded", "failed"}:
        return bulk_status
    return "skipped"


def reconcile_bulk_ingest_to_ops(
    *,
    bulk_source_id: str,
    ops_display_name: str,
    db_url: str,
    since_interval: str = "1 day",
    dry_run: bool = False,
) -> int:
    """Mirror new bulk_ingest.feed_ingest_runs rows into ops.data_source_ingest_runs.

    Returns the number of rows mirrored (0 if everything already in sync).
    Idempotency: each mirrored row carries run_metadata->>'mirrored_from_bulk_run_id' so
    re-runs only mirror genuinely-new rows.
    """
    with psycopg.connect(db_url, autocommit=True) as conn:
        ops_source_id = _resolve_ops_source_id(conn, ops_display_name)
        if ops_source_id is None:
            log.warning(
                "skipping reconciliation: ops display_name %r absent from ops.data_sources",
                ops_display_name,
            )
            return 0

        # Pull unmirrored bulk_ingest rows for this source over the window.
        rows = conn.execute(
            """
            SELECT b.run_id, b.status, b.outcome, b.started_at, b.completed_at,
                   b.rows_loaded, b.error_message, b.feed_name, b.feed_date
              FROM bulk_ingest.feed_ingest_runs b
             WHERE b.source_id = %s
               AND b.started_at >= NOW() - %s::interval
               AND NOT EXISTS (
                     SELECT 1
                       FROM ops.data_source_ingest_runs o
                      WHERE o.run_metadata->>'mirrored_from_bulk_run_id' = b.run_id::text
                   )
            """,
            (bulk_source_id, since_interval),
        ).fetchall()

        if not rows:
            log.info(
                "bulk→ops reconciliation: 0 new rows for %s (window=%s)",
                bulk_source_id,
                since_interval,
            )
            return 0

        if dry_run:
            log.info(
                "DRY-RUN: would mirror %d bulk_ingest rows for %s",
                len(rows),
                bulk_source_id,
            )
            return len(rows)

        for bulk_run_id, bulk_status, outcome, started_at, completed_at, rows_loaded, err, feed_name, feed_date in rows:
            ops_status = _bulk_status_to_ops_status(bulk_status, outcome)
            run_metadata = {
                "mirrored_from_bulk_run_id": str(bulk_run_id),
                "mirrored_at": datetime.now(timezone.utc).isoformat(),
                "bulk_source_id": bulk_source_id,
                "bulk_outcome": outcome,
                "feed_name": feed_name,
                "feed_date": feed_date.isoformat() if feed_date else None,
                "rows_loaded": rows_loaded,
            }
            conn.execute(
                """
                INSERT INTO ops.data_source_ingest_runs
                    (source_id, started_at, completed_at, status,
                     rows_ingested, run_metadata, error_message)
                VALUES (%s, %s, %s, %s::data_source_run_status,
                        %s, %s::jsonb, %s)
                """,
                (
                    ops_source_id,
                    started_at,
                    completed_at,
                    ops_status,
                    rows_loaded,
                    json.dumps(run_metadata),
                    err,
                ),
            )
        log.info(
            "bulk→ops reconciliation: mirrored %d rows for %s → %s",
            len(rows),
            bulk_source_id,
            ops_display_name,
        )
        return len(rows)


def reconcile_all_usaspending(db_url: str, since_interval: str = "1 day", dry_run: bool = False) -> int:
    """Iterate over USASPENDING_LEDGER_MAP, mirror everything."""
    total = 0
    for bulk_src, ops_name in USASPENDING_LEDGER_MAP.items():
        total += reconcile_bulk_ingest_to_ops(
            bulk_source_id=bulk_src,
            ops_display_name=ops_name,
            db_url=db_url,
            since_interval=since_interval,
            dry_run=dry_run,
        )
    return total


__all__ = [
    "USASPENDING_LEDGER_MAP",
    "reconcile_bulk_ingest_to_ops",
    "reconcile_all_usaspending",
]
