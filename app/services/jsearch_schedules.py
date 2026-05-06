"""JSearch schedule CRUD over business.jsearch_search_schedules + Trigger.dev.

Two-system invariant: each schedule_id row pairs with one Trigger.dev
schedule entity (trigger_schedule_id). Mutations apply to both atomically:
rollback DB on Trigger.dev failure during create; soft-fail Trigger.dev
delete (warn + continue) so a flaky network can't strand a schedule the
operator wants gone.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services import trigger_dev_client

log = logging.getLogger(__name__)

JSEARCH_SCHEDULED_INGEST_TASK_ID = "jsearch-scheduled-ingest"


class JSearchScheduleError(Exception):
    pass


class JSearchScheduleNotFound(JSearchScheduleError):
    pass


_COLUMNS = (
    "schedule_id, query, num_pages, country, language, "
    "date_posted, work_from_home, employment_types, job_requirements, "
    "radius, exclude_job_publishers, "
    "cron_expr, timezone, label, "
    "trigger_schedule_id, is_active, last_fired_at, last_run_id, "
    "created_at, updated_at"
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = [c.strip() for c in _COLUMNS.split(",")]
    out = dict(zip(keys, row))
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, UUID):
            out[k] = str(v)
    return out


async def create_schedule(
    *,
    query: str,
    cron_expr: str,
    timezone: str = "UTC",
    label: str | None = None,
    num_pages: int = 1,
    country: str = "us",
    language: str | None = "en",
    date_posted: str | None = None,
    work_from_home: bool = False,
    employment_types: str | None = None,
    job_requirements: str | None = None,
    radius: float | None = None,
    exclude_job_publishers: str | None = None,
) -> dict[str, Any]:
    """Insert a row, register the Trigger.dev schedule, then update the
    row with trigger_schedule_id. Rollback the row on Trigger failure."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.jsearch_search_schedules
                  (query, num_pages, country, language, date_posted,
                   work_from_home, employment_types, job_requirements,
                   radius, exclude_job_publishers,
                   cron_expr, timezone, label,
                   trigger_schedule_id, is_active)
                VALUES
                  (%s, %s, %s, %s, %s,
                   %s, %s, %s,
                   %s, %s,
                   %s, %s, %s,
                   %s, true)
                RETURNING schedule_id
                """,
                (
                    query, num_pages, country, language, date_posted,
                    work_from_home, employment_types, job_requirements,
                    radius, exclude_job_publishers,
                    cron_expr, timezone, label,
                    "_pending_",
                ),
            )
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("INSERT did not return schedule_id")
            schedule_id: UUID = row[0]

        # Register with Trigger.dev. Roll back on failure.
        try:
            trigger_resp = await trigger_dev_client.create_schedule(
                task=JSEARCH_SCHEDULED_INGEST_TASK_ID,
                cron=cron_expr,
                timezone=timezone,
                external_id=str(schedule_id),
                deduplication_key=str(schedule_id),
            )
        except Exception:
            log.exception("trigger.dev create_schedule failed; rolling back DB row")
            await conn.rollback()
            raise

        trigger_schedule_id = trigger_resp.get("id")
        if not trigger_schedule_id:
            log.error("trigger.dev returned no id; rolling back. body=%r", trigger_resp)
            await conn.rollback()
            raise RuntimeError("trigger.dev did not return a schedule id")

        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE business.jsearch_search_schedules "
                f"SET trigger_schedule_id = %s, updated_at = now() "
                f"WHERE schedule_id = %s "
                f"RETURNING {_COLUMNS}",
                (trigger_schedule_id, schedule_id),
            )
            full_row = await cur.fetchone()
        await conn.commit()
        if full_row is None:
            raise RuntimeError("post-update fetch returned no row")
        return _row_to_dict(full_row)


async def list_schedules(*, active_only: bool = False) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_COLUMNS} FROM business.jsearch_search_schedules"
        + (" WHERE is_active = true" if active_only else "")
        + " ORDER BY created_at DESC"
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM business.jsearch_search_schedules "
                f"WHERE schedule_id = %s",
                (schedule_id,),
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def delete_schedule(schedule_id: str) -> bool:
    """Delete the row + best-effort delete the Trigger.dev entity. Returns
    True if a row was deleted, False if not found. Tolerates Trigger.dev
    delete failures (warns and continues)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT trigger_schedule_id FROM business.jsearch_search_schedules "
                "WHERE schedule_id = %s",
                (schedule_id,),
            )
            row = await cur.fetchone()
            if not row:
                return False
            trigger_schedule_id = row[0]

            await cur.execute(
                "DELETE FROM business.jsearch_search_schedules WHERE schedule_id = %s",
                (schedule_id,),
            )
        await conn.commit()

    if trigger_schedule_id and trigger_schedule_id != "_pending_":
        try:
            await trigger_dev_client.delete_schedule(trigger_schedule_id)
        except Exception:
            log.exception(
                "trigger.dev delete_schedule failed for %s; "
                "DB row already removed, leaving Trigger entity orphan",
                trigger_schedule_id,
            )
    return True


async def update_last_fired(schedule_id: str, run_id: str | None) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE business.jsearch_search_schedules "
                "SET last_fired_at = now(), last_run_id = %s, updated_at = now() "
                "WHERE schedule_id = %s",
                (run_id, schedule_id),
            )
        await conn.commit()
