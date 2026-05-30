"""Per-feed Modal ingest run manifest writer.

Wraps writes to bulk_ingest.feed_ingest_runs scoped to source_id='fmcsa'
(rewired from entities.fmcsa_ingest_runs per the FMCSA backend rewire
directive — single global ledger across all bulk-ingest sources). The
orchestrator and per-feed worker functions in fmcsa_ingest_app.py call
into this module; tests cover the SQL shape and state-transition logic
in isolation by patching connect_db.

Interface contract: directive 135 reads from this table to drive the
post-ingest refresh DAG. The column set and status enum are locked.
The outcome enum (5-value: never_ran/probe_said_no_change/
succeeded_with_zero_new_rows/succeeded_with_changes/failed) and
evidence JSONB are the operator-facing front-of-house contract;
bulk_ingest.feed_dispatch_state classifies on top of them.

Landing-zone routing: mark_completed accepts landing_zone + R2 metadata
columns. When the worker writes payload to R2 instead of Postgres
(per bulk_ingest.feed_schedule_config.landing_zone, default 'r2' for
all FMCSA feeds), it passes r2_bucket / r2_object_key / payload_format /
payload_bytes to mark_completed; the ledger row records where the
payload landed even though the row itself stays in Postgres.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .postgres_writer import connect_db

MANIFEST_TABLE = "bulk_ingest.feed_ingest_runs"
SOURCE_ID = "fmcsa"

ERROR_CLASSES: frozenset[str] = frozenset(
    {
        "download_failure",
        "parse_failure",
        "db_failure",
        "timeout",
        "dup_dispatch",
        "orchestrator_dropped",
        "probe_failure",
        "r2_failure",
        "unknown",
    }
)

VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "timed_out", "skipped"}
)

OUTCOMES: frozenset[str] = frozenset(
    {
        "never_ran",
        "probe_said_no_change",
        "succeeded_with_zero_new_rows",
        "succeeded_with_changes",
        "failed",
    }
)

ERROR_MESSAGE_MAX_LEN = 4000


def _evidence_json(evidence: dict[str, Any] | None) -> str:
    """Serialize evidence dict for psycopg JSONB binding. None → '{}'."""
    return json.dumps(evidence or {}, default=str)


def classify_exception(exc: BaseException) -> str:
    """Map a Python exception to one of the ERROR_CLASSES taxonomy values.

    Heuristic only — orchestrator side. Workers can override if they
    have better signal at the call site.
    """
    message = (str(exc) or "").lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    type_name = type(exc).__name__.lower()
    module_name = (type(exc).__module__ or "").lower()
    if "psycopg" in module_name or "operationalerror" in type_name or "integrityerror" in type_name:
        return "db_failure"
    if "httpx" in module_name or "httperror" in type_name or "connectionerror" in type_name or "urlerror" in type_name:
        return "download_failure"
    if type_name in {"valueerror", "keyerror", "typeerror"} or "decode" in type_name or "json" in type_name or "csv" in module_name:
        return "parse_failure"
    return "unknown"


def insert_pending_row(
    *,
    run_id: str,
    feed_name: str,
    feed_date: str | None,
    attempt: int = 1,
    worker_concurrency_at_dispatch: int | None = None,
) -> bool:
    """Insert a 'pending' row for (run_id, feed_name, attempt). Idempotent.

    Returns True if a row was inserted, False if the (run_id, feed_name,
    attempt) triple already existed.
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MANIFEST_TABLE}
                  (run_id, source_id, feed_name, feed_date, attempt, status,
                   worker_concurrency_at_dispatch)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO NOTHING
                """,
                (run_id, SOURCE_ID, feed_name, feed_date, attempt, worker_concurrency_at_dispatch),
            )
            inserted = cursor.rowcount > 0
        connection.commit()
    return inserted


def mark_running(
    *,
    run_id: str,
    feed_name: str,
    attempt: int = 1,
    source_task_id: str | None = None,
) -> None:
    """Flip (run_id, feed_name, attempt) to 'running' and stamp started_at = NOW().

    Upserts a row if no pending placeholder exists (so this is safe to
    call from a worker that wasn't pre-seeded).
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MANIFEST_TABLE}
                  (run_id, source_id, feed_name, attempt, status, started_at, source_task_id)
                VALUES (%s, %s, %s, %s, 'running', NOW(), %s)
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE
                SET
                    status = 'running',
                    started_at = COALESCE({MANIFEST_TABLE}.started_at, NOW()),
                    source_task_id = COALESCE(EXCLUDED.source_task_id, {MANIFEST_TABLE}.source_task_id),
                    updated_at = NOW()
                """,
                (run_id, SOURCE_ID, feed_name, attempt, source_task_id),
            )
        connection.commit()


def mark_completed(
    *,
    run_id: str,
    feed_name: str,
    attempt: int = 1,
    rows_loaded: int | None = None,
    bytes_downloaded: int | None = None,
    source_task_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    landing_zone: str = "postgres",
    r2_bucket: str | None = None,
    r2_object_key: str | None = None,
    payload_format: str | None = None,
    payload_bytes: int | None = None,
) -> None:
    """Flip the row to 'completed' and stamp completed_at + duration.

    Sets outcome = succeeded_with_changes if rows_loaded > 0, else
    succeeded_with_zero_new_rows. evidence dict is merged into the
    existing JSONB (worker-side keys take precedence over heartbeat-side).

    landing_zone + r2_* + payload_* columns record where the payload
    landed. Default 'postgres' for backward-compat with workers that
    haven't yet been wired to R2; R2-routed workers pass the metadata
    they got from R2Landing.write_batch().
    """
    outcome = (
        "succeeded_with_changes"
        if (rows_loaded or 0) > 0
        else "succeeded_with_zero_new_rows"
    )
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {MANIFEST_TABLE}
                SET
                    status = 'completed',
                    outcome = %s,
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    rows_loaded = %s,
                    bytes_downloaded = %s,
                    landing_zone = %s,
                    r2_bucket = %s,
                    r2_object_key = %s,
                    payload_format = %s,
                    payload_bytes = %s,
                    source_task_id = COALESCE(%s, source_task_id),
                    evidence = COALESCE(evidence, '{{}}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE run_id = %s AND source_id = %s AND feed_name = %s AND attempt = %s
                """,
                (
                    outcome,
                    rows_loaded,
                    bytes_downloaded,
                    landing_zone,
                    r2_bucket,
                    r2_object_key,
                    payload_format,
                    payload_bytes,
                    source_task_id,
                    _evidence_json(evidence),
                    run_id,
                    SOURCE_ID,
                    feed_name,
                    attempt,
                ),
            )
        connection.commit()


def mark_failed(
    *,
    run_id: str,
    feed_name: str,
    attempt: int = 1,
    error_message: str,
    error_class: str = "unknown",
    source_task_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Flip the row to status='failed' / outcome='failed' and capture
    error_message + error_class.

    error_message is truncated to ERROR_MESSAGE_MAX_LEN. Unknown
    error_class values are coerced to 'unknown'. Failure granularity
    (probe vs worker vs dispatch) lives in error_class — the dispatch
    view routes error_class='probe_failure' to PROBE_ERROR and the rest
    to FAILED.
    """
    truncated = (error_message or "")[:ERROR_MESSAGE_MAX_LEN]
    if error_class not in ERROR_CLASSES:
        error_class = "unknown"
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {MANIFEST_TABLE}
                SET
                    status = 'failed',
                    outcome = 'failed',
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    error_message = %s,
                    error_class = %s,
                    source_task_id = COALESCE(%s, source_task_id),
                    evidence = COALESCE(evidence, '{{}}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE run_id = %s AND source_id = %s AND feed_name = %s AND attempt = %s
                """,
                (
                    truncated,
                    error_class,
                    source_task_id,
                    _evidence_json(evidence),
                    run_id,
                    SOURCE_ID,
                    feed_name,
                    attempt,
                ),
            )
        connection.commit()


def mark_probe_no_change(
    *,
    run_id: str,
    feed_name: str,
    feed_date: str | None,
    attempt: int = 1,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record that the heartbeat probe said upstream had no new data.

    Inserts a row with status='skipped' and outcome='probe_said_no_change'
    so /admin/ingest classifies the feed as SHIPPED (the operator's spec
    treats probe-said-no-change as a healthy outcome). Without this write,
    a feed whose probe stays cold for days would be indistinguishable
    from a feed the orchestrator dropped (MISSING).
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MANIFEST_TABLE}
                  (run_id, source_id, feed_name, feed_date, attempt, status, outcome,
                   started_at, completed_at, evidence)
                VALUES (%s, %s, %s, %s, %s, 'skipped', 'probe_said_no_change',
                        NOW(), NOW(), %s::jsonb)
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE
                SET
                    status = 'skipped',
                    outcome = 'probe_said_no_change',
                    completed_at = NOW(),
                    evidence = COALESCE({MANIFEST_TABLE}.evidence, '{{}}'::jsonb) || EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    SOURCE_ID,
                    feed_name,
                    feed_date,
                    attempt,
                    _evidence_json(evidence),
                ),
            )
        connection.commit()


def record_dup_dispatch(
    *,
    run_id: str,
    feed_name: str,
    feed_date: str | None,
    attempt: int = 1,
    winning_run_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record an orchestrator-side dup-dispatch suppression.

    Surfaces the in-flight idempotency check from PR #200 to the operator.
    outcome='probe_said_no_change' (NOT 'failed') because the singleton-
    cancel guard firing is healthy behavior — the SECOND dispatch was
    redundant, the FIRST is doing the work. Misclassifying as 'failed'
    pollutes the failure rate (audit 2026-05-08 found 27 such rows
    inflating the prod failure count). error_class='dup_dispatch' is
    preserved as the forensic marker.
    """
    payload: dict[str, Any] = {
        "winning_run_id": winning_run_id,
        "suppressed_by": "orchestrator_in_flight_check",
    }
    if evidence:
        payload.update(evidence)
    error_message = (
        f"Modal-retry duplicate suppressed; winning run_id={winning_run_id}"
        if winning_run_id
        else "Modal-retry duplicate suppressed; another run already in-flight"
    )
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MANIFEST_TABLE}
                  (run_id, source_id, feed_name, feed_date, attempt, status, outcome,
                   error_class, error_message, started_at, completed_at, evidence)
                VALUES (%s, %s, %s, %s, %s, 'skipped', 'probe_said_no_change',
                        'dup_dispatch', %s, NOW(), NOW(), %s::jsonb)
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE
                SET
                    status = 'skipped',
                    outcome = 'probe_said_no_change',
                    error_class = 'dup_dispatch',
                    error_message = EXCLUDED.error_message,
                    completed_at = NOW(),
                    evidence = COALESCE({MANIFEST_TABLE}.evidence, '{{}}'::jsonb) || EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    SOURCE_ID,
                    feed_name,
                    feed_date,
                    attempt,
                    error_message,
                    _evidence_json(payload),
                ),
            )
        connection.commit()


def mark_lingering_as_timed_out(*, run_id: str) -> int:
    """Reaper: flip every still-'running' row for run_id to 'timed_out'.

    Called at the end of the orchestrator's window. Returns the number of
    rows reaped. Outcome maps to 'failed' so the dashboard surfaces it as
    FAILED rather than as a missing row.
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {MANIFEST_TABLE}
                SET
                    status = 'timed_out',
                    outcome = 'failed',
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    error_class = 'timeout',
                    error_message = COALESCE(
                        error_message,
                        'orchestrator window closed before worker reported completion'
                    ),
                    evidence = COALESCE(evidence, '{{}}'::jsonb) || jsonb_build_object(
                        'reaped_by', 'mark_lingering_as_timed_out',
                        'reaped_at', NOW()
                    ),
                    updated_at = NOW()
                WHERE run_id = %s AND source_id = %s AND status = 'running'
                """,
                (run_id, SOURCE_ID),
            )
            count = cursor.rowcount
        connection.commit()
    return count


def load_failed_feed_names(*, prior_run_id: str) -> list[tuple[str, int]]:
    """Return [(feed_name, last_attempt), ...] for failed/timed_out feeds in a prior run.

    Used by retry_failed_feeds; the orchestrator will re-spawn each at
    last_attempt + 1.
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT feed_name, MAX(attempt) AS last_attempt
                FROM {MANIFEST_TABLE}
                WHERE run_id = %s AND source_id = %s AND status IN ('failed','timed_out')
                GROUP BY feed_name
                ORDER BY feed_name
                """,
                (prior_run_id, SOURCE_ID),
            )
            rows = cursor.fetchall() or []
    return [(row["feed_name"], int(row["last_attempt"])) for row in rows]


def status_rollup(*, run_id: str) -> dict[str, int]:
    """Return per-status counts for a run_id with all 6 statuses present (zero-default).

    Used by directive 135 to determine when a run is complete.
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT status, COUNT(*)::bigint AS cnt
                FROM {MANIFEST_TABLE}
                WHERE run_id = %s AND source_id = %s
                GROUP BY status
                """,
                (run_id, SOURCE_ID),
            )
            rows = cursor.fetchall() or []
    rollup: dict[str, int] = {status: 0 for status in VALID_STATUSES}
    for row in rows:
        rollup[row["status"]] = int(row["cnt"])
    return rollup


def list_feeds_with_status(
    *,
    run_id: str,
    statuses: Iterable[str],
) -> list[dict[str, Any]]:
    """Return manifest rows for the run filtered to the given statuses."""
    statuses_list = list(statuses)
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    feed_name,
                    attempt,
                    status,
                    error_class,
                    error_message,
                    started_at,
                    completed_at,
                    duration_seconds,
                    rows_loaded
                FROM {MANIFEST_TABLE}
                WHERE run_id = %s AND source_id = %s AND status = ANY(%s)
                ORDER BY feed_name, attempt
                """,
                (run_id, SOURCE_ID, statuses_list),
            )
            rows = cursor.fetchall() or []
    return list(rows)
