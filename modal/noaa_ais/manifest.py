"""ops.ais_pings_ingest_runs — file-level idempotency for NOAA AIS daily CSV.

The manifest table is the single source of truth for "has this NOAA file been
landed?". Schema is defined in
20260505220000_create_source_ais_pings.sql; this module is the writer.

Workflow:

    1. open_run(source_filename, ...) — INSERT a 'running' row.
       - If a 'succeeded' row already exists for source_filename → returns None
         (caller skips the day).
       - If a 'running' or 'failed' row exists → it is replaced (rerun).
    2. mark_succeeded(run_id, rows_loaded, ...) — flip to 'succeeded' on R2 write.
    3. mark_failed(run_id, error_text) — flip to 'failed' on any exception.

Per-row provenance columns (r2_bucket, r2_object_key, payload_bytes,
payload_format) are stored in source_run_metadata jsonb so we don't have to
add columns; if surfaced volumetrically the migration can promote them.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

# Modal secret 'noaa-ais-db' is expected to carry DATABASE_URL pointing at
# the data-engine-x Postgres (DEX_DB_URL_POOLED in Doppler). Set up via:
#   doppler run --project hq-all --config prd -- bash -c '
#       modal secret create --force noaa-ais-db DATABASE_URL="$DEX_DB_URL_POOLED"
#   '
# The dex-db secret follows the same convention.
_DB_URL_ENV = "DATABASE_URL"


def _connect() -> psycopg.Connection:
    url = os.environ.get(_DB_URL_ENV) or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            f"{_DB_URL_ENV} / DEX_DB_URL_POOLED unset — Modal secret "
            f"'noaa-ais-db' must inject one of them."
        )
    return psycopg.connect(url, row_factory=dict_row)


def _existing_status(*, source_filename: str) -> str | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM ops.ais_pings_ingest_runs
                WHERE source_filename = %s
                """,
                (source_filename,),
            )
            row = cur.fetchone()
    return row["status"] if row else None


def open_run(
    *,
    source_filename: str,
    source_download_url: str,
    file_date: date,
    source_observed_at: datetime | None = None,
    task_id: str | None = None,
    schedule_id: str | None = None,
) -> UUID | None:
    """INSERT a fresh 'running' row, or return None if already 'succeeded'.

    For 'failed'/'running' prior rows we DELETE-and-INSERT — single-operator
    backfill, no concurrent runs against the same source_filename. If two
    operators race on the same file, the UNIQUE (source_filename) constraint
    will surface the loser as a psycopg.errors.UniqueViolation.
    """
    prior = _existing_status(source_filename=source_filename)
    if prior == "succeeded":
        return None

    run_id = uuid4()
    with _connect() as conn:
        with conn.cursor() as cur:
            if prior is not None:
                cur.execute(
                    """
                    DELETE FROM ops.ais_pings_ingest_runs
                    WHERE source_filename = %s
                    """,
                    (source_filename,),
                )
            cur.execute(
                """
                INSERT INTO ops.ais_pings_ingest_runs (
                    run_id, status, source_provider, source_filename,
                    source_download_url, source_observed_at,
                    file_date, task_id, schedule_id, started_at
                )
                VALUES (
                    %s, 'running', 'noaa_ais', %s, %s, %s,
                    %s, %s, %s, NOW()
                )
                """,
                (
                    str(run_id),
                    source_filename,
                    source_download_url,
                    source_observed_at,
                    file_date,
                    task_id,
                    schedule_id,
                ),
            )
        conn.commit()
    return run_id


def mark_succeeded(
    *,
    run_id: UUID,
    rows_loaded: int,
    r2_bucket: str | None,
    r2_object_key: str | None,
    payload_bytes: int | None,
    payload_format: str | None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    metadata = {
        "r2_bucket": r2_bucket,
        "r2_object_key": r2_object_key,
        "payload_bytes": payload_bytes,
        "payload_format": payload_format,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.ais_pings_ingest_runs
                SET status = 'succeeded',
                    completed_at = NOW(),
                    rows_loaded = %s,
                    source_run_metadata = %s::jsonb
                WHERE run_id = %s
                """,
                (rows_loaded, json.dumps(metadata), str(run_id)),
            )
        conn.commit()


def mark_failed(
    *,
    run_id: UUID,
    error_text: str,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    metadata = extra_metadata or {}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.ais_pings_ingest_runs
                SET status = 'failed',
                    completed_at = NOW(),
                    error_text = %s,
                    source_run_metadata = COALESCE(source_run_metadata, '{}'::jsonb)
                                          || %s::jsonb
                WHERE run_id = %s
                """,
                (error_text[:8000], json.dumps(metadata), str(run_id)),
            )
        conn.commit()
