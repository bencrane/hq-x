"""Direct-mail funnel analytics — port of OEX's ``/direct-mail`` endpoint
to hq-x's organization model.

Aggregates ``direct_mail_pieces`` and ``direct_mail_piece_events`` over a
time window into:

* totals (pieces, delivered, in_transit, returned, failed, test_mode_count)
* funnel (queued / processed / in_transit / delivered / returned / failed)
* by_piece_type (postcard / letter / self_mailer / snap_pack / booklet)
* daily_trends (day-bucketed created / delivered / failed counts, prefilled
  for every day in the window)
* failure_reason_breakdown (top reasons from
  ``direct_mail_piece_events`` for failed/returned events, capped at 50)

Filter scope:

* ``organization_id`` — always from auth context, never from the request.
* ``brand_id`` — optional drilldown; must belong to the auth's org or 404.
* ``channel_campaign_id`` — optional drilldown; must belong to the auth's
  org or 404.
* ``channel_campaign_step_id`` — optional drilldown; must belong to the
  auth's org or 404.

Org isolation: ``direct_mail_pieces`` doesn't carry ``organization_id``
directly; the SQL joins through ``business.brands.organization_id``
(every piece has a ``brand_id``) so the query plan reads only rows in
the caller's org.

Postgres-only; payload always carries ``"source": "postgres"``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.db import get_db_connection


class DirectMailFilterNotFound(Exception):
    """Raised when an optional filter (brand / channel_campaign / step) does
    not belong to the caller's organization."""


_PIECE_FUNNEL_KEYS = (
    "queued",
    "processed",
    "in_transit",
    "delivered",
    "returned",
    "failed",
)

_PIECE_STATUS_TO_FUNNEL = {
    "queued": "queued",
    "processing": "queued",
    "created": "queued",
    "unknown": "queued",
    "processed": "processed",
    "ready_for_mail": "processed",
    "mailed": "processed",
    "in_transit": "in_transit",
    "in_local_area": "in_transit",
    "processed_for_delivery": "in_transit",
    "delivered": "delivered",
    "returned": "returned",
    "failed": "failed",
    "rejected": "failed",
}

_DAILY_KEYS = ("created", "delivered", "failed")
_DELIVERED_EVENT_TYPES = (
    "piece.delivered",
    "piece.processed_for_delivery",
)
_FAILED_EVENT_TYPES = (
    "piece.failed",
    "piece.returned",
    "piece.rejected",
)
_FAILURE_REASON_TYPES = _FAILED_EVENT_TYPES

# OEX's safety caps; hq-x mirrors them so a noisy account can't take the
# DB down.
_MAX_ROWS = 20_000
_MAX_FAILURE_BREAKDOWN = 50


async def _assert_brand_in_org(
    *, brand_id: UUID, organization_id: UUID
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM business.brands
                WHERE id = %s AND organization_id = %s LIMIT 1
                """,
                (str(brand_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise DirectMailFilterNotFound(
            f"brand {brand_id} not in organization {organization_id}"
        )


async def _assert_channel_campaign_in_org(
    *, channel_campaign_id: UUID, organization_id: UUID
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM business.channel_campaigns
                WHERE id = %s AND organization_id = %s LIMIT 1
                """,
                (str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise DirectMailFilterNotFound(
            f"channel_campaign {channel_campaign_id} not in organization "
            f"{organization_id}"
        )


async def _assert_step_in_org(
    *, channel_campaign_step_id: UUID, organization_id: UUID
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM business.channel_campaign_steps
                WHERE id = %s AND organization_id = %s LIMIT 1
                """,
                (str(channel_campaign_step_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise DirectMailFilterNotFound(
            f"channel_campaign_step {channel_campaign_step_id} not in "
            f"organization {organization_id}"
        )


def _piece_filter_clause(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    channel_campaign_id: UUID | None,
    channel_campaign_step_id: UUID | None,
) -> tuple[str, list[Any]]:
    """Build the WHERE-clause + param list shared by every piece query.

    Org isolation flows through brands.organization_id always (the join
    is in the FROM clause of each query that uses this filter).
    """
    where = [
        "p.deleted_at IS NULL",
        "b.organization_id = %s",
    ]
    params: list[Any] = [str(organization_id)]
    if brand_id is not None:
        where.append("p.brand_id = %s")
        params.append(str(brand_id))
    if channel_campaign_id is not None:
        where.append("p.channel_campaign_id = %s")
        params.append(str(channel_campaign_id))
    if channel_campaign_step_id is not None:
        where.append("p.channel_campaign_step_id = %s")
        params.append(str(channel_campaign_step_id))
    return " AND ".join(where), params


async def _load_piece_aggregates(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    channel_campaign_id: UUID | None,
    channel_campaign_step_id: UUID | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Aggregate pieces by status, type, and per-day-created over the window."""
    base_where, base_params = _piece_filter_clause(
        organization_id=organization_id,
        brand_id=brand_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )
    window_clause = "p.created_at >= %s AND p.created_at < %s"
    full_where = f"{base_where} AND {window_clause}"

    sql_status = f"""
        SELECT p.status,
               COUNT(*) AS pieces,
               COUNT(*) FILTER (WHERE p.is_test_mode) AS test_mode_count
        FROM direct_mail_pieces p
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
        GROUP BY p.status
    """
    sql_type = f"""
        SELECT p.piece_type,
               COUNT(*) AS pieces,
               COUNT(*) FILTER (WHERE p.status = 'delivered')
                 AS delivered,
               COUNT(*) FILTER (WHERE p.status IN ('failed', 'returned', 'rejected'))
                 AS failed
        FROM direct_mail_pieces p
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
        GROUP BY p.piece_type
        ORDER BY p.piece_type
    """
    sql_count_check = f"""
        SELECT COUNT(*) FROM direct_mail_pieces p
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
    """
    sql_daily_created = f"""
        SELECT (p.created_at AT TIME ZONE 'UTC')::date AS day,
               COUNT(*) AS created
        FROM direct_mail_pieces p
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
        GROUP BY day
        ORDER BY day
    """

    args = [*base_params, start, end]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql_count_check, args)
            count_row = await cur.fetchone()
            row_count = int(count_row[0]) if count_row else 0
            if row_count > _MAX_ROWS:
                raise ValueError(
                    f"piece row count {row_count} exceeds max {_MAX_ROWS}"
                )

            await cur.execute(sql_status, args)
            status_rows = await cur.fetchall()
            await cur.execute(sql_type, args)
            type_rows = await cur.fetchall()
            await cur.execute(sql_daily_created, args)
            daily_created_rows = await cur.fetchall()

    return {
        "status_rows": status_rows,
        "type_rows": type_rows,
        "daily_created": daily_created_rows,
        "total_pieces": row_count,
    }


async def _load_event_aggregates(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    channel_campaign_id: UUID | None,
    channel_campaign_step_id: UUID | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Aggregate direct_mail_piece_events for daily delivered/failed and
    failure-reason breakdown.

    The events join goes through ``direct_mail_pieces`` so the same org-
    isolation guard via ``business.brands.organization_id`` applies.
    """
    base_where, base_params = _piece_filter_clause(
        organization_id=organization_id,
        brand_id=brand_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )
    full_where = (
        f"{base_where} "
        "AND e.received_at >= %s AND e.received_at < %s"
    )
    args = [*base_params, start, end]

    sql_daily_events = f"""
        SELECT (e.received_at AT TIME ZONE 'UTC')::date AS day,
               e.event_type,
               COUNT(*) AS cnt
        FROM direct_mail_piece_events e
        JOIN direct_mail_pieces p ON p.id = e.piece_id
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
        GROUP BY day, e.event_type
        ORDER BY day, e.event_type
    """
    failure_event_clause = ", ".join(
        ["%s"] * len(_FAILURE_REASON_TYPES)
    )
    sql_failure_reasons = f"""
        SELECT COALESCE(e.raw_payload->>'reason', e.event_type) AS reason,
               COUNT(*) AS cnt
        FROM direct_mail_piece_events e
        JOIN direct_mail_pieces p ON p.id = e.piece_id
        JOIN business.brands b ON b.id = p.brand_id
        WHERE {full_where}
          AND e.event_type IN ({failure_event_clause})
        GROUP BY reason
        ORDER BY cnt DESC, reason
        LIMIT %s
    """
    failure_args = [
        *args,
        *_FAILURE_REASON_TYPES,
        _MAX_FAILURE_BREAKDOWN,
    ]

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql_daily_events, args)
            daily_event_rows = await cur.fetchall()
            await cur.execute(sql_failure_reasons, failure_args)
            failure_rows = await cur.fetchall()

    return {
        "daily_events": daily_event_rows,
        "failure_reasons": failure_rows,
    }


def _build_funnel(status_rows: list[tuple]) -> dict[str, int]:
    funnel = {k: 0 for k in _PIECE_FUNNEL_KEYS}
    for status_, pieces, _ in status_rows:
        bucket = _PIECE_STATUS_TO_FUNNEL.get(status_)
        if bucket is not None:
            funnel[bucket] += int(pieces)
    return funnel


def _build_totals(status_rows: list[tuple]) -> dict[str, int]:
    totals = {
        "pieces": 0,
        "delivered": 0,
        "in_transit": 0,
        "returned": 0,
        "failed": 0,
        "test_mode_count": 0,
    }
    for status_, pieces, test_mode_count in status_rows:
        n = int(pieces)
        totals["pieces"] += n
        totals["test_mode_count"] += int(test_mode_count or 0)
        bucket = _PIECE_STATUS_TO_FUNNEL.get(status_)
        if bucket == "delivered":
            totals["delivered"] += n
        elif bucket == "in_transit":
            totals["in_transit"] += n
        elif bucket == "returned":
            totals["returned"] += n
        elif bucket == "failed":
            totals["failed"] += n
    return totals


def _build_daily_trends(
    *,
    start: datetime,
    end: datetime,
    daily_created_rows: list[tuple],
    daily_event_rows: list[tuple],
) -> list[dict[str, Any]]:
    bucket: dict[str, dict[str, int]] = {}
    day_cursor = start.date()
    end_day = end.date()
    while day_cursor <= end_day:
        bucket[day_cursor.isoformat()] = {k: 0 for k in _DAILY_KEYS}
        day_cursor += timedelta(days=1)

    for day, created in daily_created_rows:
        key = day.isoformat()
        if key in bucket:
            bucket[key]["created"] = int(created)

    for day, event_type, cnt in daily_event_rows:
        key = day.isoformat()
        if key not in bucket:
            continue
        if event_type in _DELIVERED_EVENT_TYPES:
            bucket[key]["delivered"] += int(cnt)
        elif event_type in _FAILED_EVENT_TYPES:
            bucket[key]["failed"] += int(cnt)

    return [{"date": day, **counts} for day, counts in sorted(bucket.items())]


async def summarize_direct_mail(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    channel_campaign_id: UUID | None,
    channel_campaign_step_id: UUID | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Build the direct-mail analytics payload.

    Optional filters are pre-validated for org membership; an
    out-of-org filter raises :class:`DirectMailFilterNotFound` (router
    maps to 404).
    """
    if brand_id is not None:
        await _assert_brand_in_org(
            brand_id=brand_id, organization_id=organization_id
        )
    if channel_campaign_id is not None:
        await _assert_channel_campaign_in_org(
            channel_campaign_id=channel_campaign_id,
            organization_id=organization_id,
        )
    if channel_campaign_step_id is not None:
        await _assert_step_in_org(
            channel_campaign_step_id=channel_campaign_step_id,
            organization_id=organization_id,
        )

    pieces = await _load_piece_aggregates(
        organization_id=organization_id,
        brand_id=brand_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=channel_campaign_step_id,
        start=start,
        end=end,
    )
    events = await _load_event_aggregates(
        organization_id=organization_id,
        brand_id=brand_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=channel_campaign_step_id,
        start=start,
        end=end,
    )

    totals = _build_totals(pieces["status_rows"])
    funnel = _build_funnel(pieces["status_rows"])
    by_piece_type = [
        {
            "piece_type": piece_type,
            "count": int(count),
            "delivered": int(delivered),
            "failed": int(failed),
        }
        for piece_type, count, delivered, failed in pieces["type_rows"]
    ]
    failure_reason_breakdown = [
        {"reason": reason, "count": int(count)}
        for reason, count in events["failure_reasons"]
    ]
    daily_trends = _build_daily_trends(
        start=start,
        end=end,
        daily_created_rows=pieces["daily_created"],
        daily_event_rows=events["daily_events"],
    )

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "totals": totals,
        "funnel": funnel,
        "by_piece_type": by_piece_type,
        "daily_trends": daily_trends,
        "failure_reason_breakdown": failure_reason_breakdown,
        "source": "postgres",
    }


__all__ = ["DirectMailFilterNotFound", "summarize_direct_mail"]
