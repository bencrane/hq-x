from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .feed_catalog import FeedConfig
from .mappings import (
    SNAPSHOT_HISTORY_TABLES,
    ParsedSourceRow,
    build_record_fingerprint,
    build_typed_row,
)

ENTITIES_SCHEMA = "entities"
CONFLICT_COLUMNS = ("feed_date", "source_feed_name", "row_position")
LEGACY_CONFLICT_COLUMNS = ("record_fingerprint",)
INSERT_ONLY_ON_CONFLICT_COLUMNS = frozenset({"created_at", "record_fingerprint", "first_observed_at"})
JSONB_COLUMNS = frozenset({"raw_source_row", "source_run_metadata"})
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COPY_NULL_TOKEN = r"\N"
COPY_DELIMITER = "\t"
COPY_ROW_DELIMITER = "\n"


@dataclass(frozen=True)
class PersistContext:
    feed_date: str
    source_observed_at: str
    source_task_id: str
    source_schedule_id: str | None
    source_run_metadata: dict[str, Any]
    use_snapshot_replace: bool
    is_first_chunk: bool


def _quote_identifier(identifier: str) -> str:
    if not SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier}")
    return f'"{identifier}"'


def _quote_qualified_identifier(*parts: str) -> str:
    return ".".join(_quote_identifier(part) for part in parts)


def _build_temp_table_name(table_name: str) -> str:
    sanitized_table_name = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
    suffix = uuid4().hex[:8]
    max_table_name_length = 63 - len("tmp_fmcsa__") - len(suffix)
    truncated_table_name = sanitized_table_name[:max_table_name_length]
    return f"tmp_fmcsa_{truncated_table_name}_{suffix}"


def _serialize_jsonb_copy_value(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _escape_copy_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _serialize_copy_value(*, column_name: str, value: Any) -> str:
    if value is None:
        return COPY_NULL_TOKEN
    if column_name in JSONB_COLUMNS and isinstance(value, (dict, list)):
        text_value = _serialize_jsonb_copy_value(value)
    elif isinstance(value, bool):
        text_value = "t" if value else "f"
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        text_value = value.astimezone(timezone.utc).isoformat()
    elif isinstance(value, date):
        text_value = value.isoformat()
    else:
        text_value = str(value)
    return _escape_copy_text(text_value)


def _build_copy_payload(*, rows: list[dict[str, Any]], columns: list[str]) -> str:
    serialized_rows: list[str] = []
    for row in rows:
        serialized_rows.append(
            COPY_DELIMITER.join(
                _serialize_copy_value(column_name=column_name, value=row.get(column_name))
                for column_name in columns
            )
        )
    return f"{COPY_ROW_DELIMITER.join(serialized_rows)}{COPY_ROW_DELIMITER}"


def _collect_insert_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column_name in row:
            if column_name not in seen:
                seen.add(column_name)
                columns.append(column_name)
    return columns


def _get_table_columns(connection: psycopg.Connection[Any], table_name: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (ENTITIES_SCHEMA, table_name),
        )
        rows = cursor.fetchall()
    return {str(row["column_name"]) for row in rows}


def _get_conflict_columns(*, available_columns: set[str]) -> tuple[str, ...]:
    if set(CONFLICT_COLUMNS).issubset(available_columns):
        return CONFLICT_COLUMNS
    if set(LEGACY_CONFLICT_COLUMNS).issubset(available_columns):
        return LEGACY_CONFLICT_COLUMNS
    raise ValueError("No valid FMCSA conflict target found in live table schema")


def _build_merge_sql(
    *,
    table_name: str,
    temp_table_name: str,
    columns: list[str],
    conflict_columns: tuple[str, ...],
    use_snapshot_replace: bool,
) -> str:
    quoted_columns = ", ".join(_quote_identifier(column_name) for column_name in columns)
    projected_columns = ", ".join(_quote_identifier(column_name) for column_name in columns)
    if use_snapshot_replace:
        return (
            f"INSERT INTO {_quote_qualified_identifier(ENTITIES_SCHEMA, table_name)} "
            f"({quoted_columns}) SELECT {projected_columns} FROM {_quote_identifier(temp_table_name)}"
        )
    quoted_conflict_columns = ", ".join(_quote_identifier(column_name) for column_name in conflict_columns)
    update_assignments = [
        f'{_quote_identifier(column_name)} = EXCLUDED.{_quote_identifier(column_name)}'
        for column_name in columns
        if column_name not in conflict_columns and column_name not in INSERT_ONLY_ON_CONFLICT_COLUMNS
    ]
    if not update_assignments:
        raise ValueError(f"No mutable columns available for {table_name}")
    return (
        f"INSERT INTO {_quote_qualified_identifier(ENTITIES_SCHEMA, table_name)} ({quoted_columns}) "
        f"SELECT {projected_columns} FROM {_quote_identifier(temp_table_name)} "
        f"ON CONFLICT ({quoted_conflict_columns}) DO UPDATE SET {', '.join(update_assignments)}"
    )


def _copy_rows_into_temp_table(
    *,
    cursor: psycopg.Cursor[Any],
    temp_table_name: str,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    quoted_columns = ", ".join(_quote_identifier(column_name) for column_name in columns)
    copy_sql = (
        f"COPY {_quote_identifier(temp_table_name)} ({quoted_columns}) "
        "FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
    )
    payload = _build_copy_payload(rows=rows, columns=columns)
    with cursor.copy(copy_sql) as copy:
        copy.write(payload)


def _build_insert_rows(
    *,
    feed: FeedConfig,
    rows: list[ParsedSourceRow],
    context: PersistContext,
    live_table_columns: set[str],
) -> list[dict[str, Any]]:
    now_iso = datetime.now(timezone.utc).isoformat()
    result: list[dict[str, Any]] = []
    for row in rows:
        typed_row = build_typed_row(feed, row)
        insert_row: dict[str, Any] = {
            **typed_row,
            "feed_date": context.feed_date,
            "row_position": row.row_number,
            "source_provider": "fmcsa_open_data",
            "source_feed_name": feed.feed_name,
            "source_download_url": feed.download_url,
            "source_file_variant": feed.source_file_variant,
            "source_observed_at": context.source_observed_at,
            "source_task_id": context.source_task_id,
            "source_schedule_id": context.source_schedule_id,
            "source_run_metadata": context.source_run_metadata,
            "raw_source_row": {
                "row_number": row.row_number,
                "raw_values": row.raw_values,
                "raw_fields": row.raw_fields,
            },
            "updated_at": now_iso,
        }
        if feed.table_name in SNAPSHOT_HISTORY_TABLES and (
            not live_table_columns or "record_fingerprint" in live_table_columns
        ):
            insert_row["record_fingerprint"] = build_record_fingerprint(
                table_name=feed.table_name,
                feed_date=context.feed_date,
                feed_name=feed.feed_name,
                row_position=row.row_number,
            )
        if feed.table_name in SNAPSHOT_HISTORY_TABLES and (
            not live_table_columns or "first_observed_at" in live_table_columns
        ):
            insert_row["first_observed_at"] = context.source_observed_at
        if feed.table_name in SNAPSHOT_HISTORY_TABLES and (
            not live_table_columns or "last_observed_at" in live_table_columns
        ):
            insert_row["last_observed_at"] = context.source_observed_at

        if live_table_columns:
            insert_row = {k: v for k, v in insert_row.items() if k in live_table_columns}
        result.append(insert_row)
    return result


def persist_chunk(
    *,
    connection: psycopg.Connection[Any],
    feed: FeedConfig,
    rows: list[ParsedSourceRow],
    context: PersistContext,
) -> int:
    if not rows:
        return 0

    live_columns = _get_table_columns(connection, feed.table_name)
    insert_rows = _build_insert_rows(feed=feed, rows=rows, context=context, live_table_columns=live_columns)
    columns = _collect_insert_columns(insert_rows)
    conflict_columns = _get_conflict_columns(available_columns=set(columns))
    temp_table_name = _build_temp_table_name(feed.table_name)
    merge_sql = _build_merge_sql(
        table_name=feed.table_name,
        temp_table_name=temp_table_name,
        columns=columns,
        conflict_columns=conflict_columns,
        use_snapshot_replace=context.use_snapshot_replace,
    )

    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '600s'")
        if context.use_snapshot_replace and context.is_first_chunk:
            cursor.execute(
                f"DELETE FROM {_quote_qualified_identifier(ENTITIES_SCHEMA, feed.table_name)} "
                f"WHERE {_quote_identifier('feed_date')} = %s AND {_quote_identifier('source_feed_name')} = %s",
                (context.feed_date, feed.feed_name),
            )

        cursor.execute(
            f"CREATE TEMP TABLE {_quote_identifier(temp_table_name)} "
            f"(LIKE {_quote_qualified_identifier(ENTITIES_SCHEMA, feed.table_name)} INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        _copy_rows_into_temp_table(cursor=cursor, temp_table_name=temp_table_name, rows=insert_rows, columns=columns)
        cursor.execute(merge_sql)

    connection.commit()
    return len(insert_rows)


def connect_db() -> psycopg.Connection[Any]:
    # Prefer the pooled DSN (DEX convention; the ingest is COPY/SELECT-only,
    # which transaction-mode pooling handles), then the direct DSN, then
    # DATABASE_URL for ad-hoc runs. Reading DATABASE_URL first was the anomaly:
    # on the shared `dex-db` secret that key holds the Supabase REST URL (not a
    # DSN), which silently broke every heartbeat tick. Mirrors the pooled-first
    # precedent in landing/ledger.py.
    database_url = (
        os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DATABASE_URL")
    )
    if not database_url:
        raise RuntimeError(
            "DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT / DATABASE_URL not set"
        )
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=6,
        options="-c statement_timeout=0 -c idle_in_transaction_session_timeout=0",
    )


def persist_all_chunks(
    *,
    feed: FeedConfig,
    chunks: Iterable[list[ParsedSourceRow]],
    feed_date: str,
    source_task_id: str,
    source_observed_at: str,
    source_schedule_id: str | None,
    source_run_metadata: dict[str, Any],
) -> dict[str, Any]:
    rows_written = 0
    chunks_processed = 0
    use_snapshot_replace = feed.source_kind in {"sms_csv", "csv_export"}
    with connect_db() as connection:
        for chunk in chunks:
            chunks_processed += 1
            chunk_rows_written = persist_chunk(
                connection=connection,
                feed=feed,
                rows=chunk,
                context=PersistContext(
                    feed_date=feed_date,
                    source_observed_at=source_observed_at,
                    source_task_id=source_task_id,
                    source_schedule_id=source_schedule_id,
                    source_run_metadata=source_run_metadata,
                    use_snapshot_replace=use_snapshot_replace,
                    is_first_chunk=chunks_processed == 1,
                ),
            )
            rows_written += chunk_rows_written
    return {
        "rows_written": rows_written,
        "chunks_processed": chunks_processed,
    }
