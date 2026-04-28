"""Voice analytics dashboard (brand-axis).

Brand-axis port of OEX ``routers/voice_analytics.py``. The OEX version
preferred ClickHouse via ``ch_query`` and fell back to Supabase. hq-x's
``app/clickhouse.py`` only exposes a fire-and-forget ``insert_row`` writer
(no query helper), so this router queries Postgres ``call_logs`` directly.
A ClickHouse query path can be layered on later without breaking the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection

router = APIRouter(
    prefix="/api/brands/{brand_id}/analytics/voice",
    tags=["voice-analytics"],
)


def _default_start() -> str:
    return (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")


def _default_end() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _date_range(start: str | None, end: str | None) -> tuple[str, str]:
    return (start or _default_start(), (end or _default_end()) + "T23:59:59Z")


@router.get("/summary")
async def voice_summary(
    brand_id: UUID,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    campaign_id: UUID | None = Query(default=None),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    start, end = _date_range(start_date, end_date)
    where = ["brand_id = %s", "deleted_at IS NULL", "created_at >= %s", "created_at <= %s"]
    values: list[Any] = [str(brand_id), start, end]
    if campaign_id:
        where.append("campaign_id = %s")
        values.append(str(campaign_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT COALESCE(outcome, status, 'unknown') as outcome,
                       COUNT(*) as cnt,
                       COALESCE(SUM(duration_seconds), 0) as total_duration,
                       COALESCE(SUM(cost_total), 0) as total_cost
                FROM call_logs
                WHERE {' AND '.join(where)}
                GROUP BY COALESCE(outcome, status, 'unknown')
                """,
                values,
            )
            rows = await cur.fetchall()
    by_outcome = [
        {"outcome": r[0], "cnt": int(r[1]), "total_duration": int(r[2]), "total_cost": float(r[3])}
        for r in rows
    ]
    return {
        "by_outcome": by_outcome,
        "total_calls": sum(b["cnt"] for b in by_outcome),
        "total_duration": sum(b["total_duration"] for b in by_outcome),
        "total_cost": sum(b["total_cost"] for b in by_outcome),
        "source": "postgres",
    }


@router.get("/by-campaign")
async def voice_by_campaign(
    brand_id: UUID,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    start, end = _date_range(start_date, end_date)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT campaign_id, COALESCE(outcome, status, 'unknown') AS outcome,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(duration_seconds), 0) AS total_duration,
                       COALESCE(SUM(cost_total), 0) AS total_cost
                FROM call_logs
                WHERE brand_id = %s AND deleted_at IS NULL
                  AND created_at >= %s AND created_at <= %s
                GROUP BY campaign_id, COALESCE(outcome, status, 'unknown')
                """,
                (str(brand_id), start, end),
            )
            rows = await cur.fetchall()
    campaigns: dict[str, dict[str, Any]] = {}
    for cid, outcome, cnt, dur, cost in rows:
        key = str(cid) if cid else ""
        bucket = campaigns.setdefault(
            key, {"campaign_id": key, "calls": 0, "duration": 0, "cost": 0.0, "outcomes": {}}
        )
        bucket["calls"] += int(cnt)
        bucket["duration"] += int(dur)
        bucket["cost"] += float(cost)
        bucket["outcomes"][outcome] = int(cnt)
    return {"campaigns": list(campaigns.values()), "source": "postgres"}


@router.get("/daily-trend")
async def voice_daily_trend(
    brand_id: UUID,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    start, end = _date_range(start_date, end_date)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,
                       COUNT(*) AS cnt
                FROM call_logs
                WHERE brand_id = %s AND deleted_at IS NULL
                  AND created_at >= %s AND created_at <= %s
                GROUP BY day ORDER BY day
                """,
                (str(brand_id), start, end),
            )
            rows = await cur.fetchall()
    return {
        "daily": [{"date": r[0], "cnt": int(r[1])} for r in rows],
        "source": "postgres",
    }


@router.get("/cost-breakdown")
async def voice_cost_breakdown(
    brand_id: UUID,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    start, end = _date_range(start_date, end_date)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT cost_breakdown, cost_total
                FROM call_logs
                WHERE brand_id = %s AND deleted_at IS NULL
                  AND created_at >= %s AND created_at <= %s
                """,
                (str(brand_id), start, end),
            )
            rows = await cur.fetchall()
    totals = {"transport": 0.0, "stt": 0.0, "llm": 0.0, "tts": 0.0, "vapi": 0.0, "total": 0.0}
    for breakdown, cost_total in rows:
        b = breakdown or {}
        for key in ("transport", "stt", "llm", "tts", "vapi"):
            totals[key] += float(b.get(key, 0) or 0)
        totals["total"] += float(cost_total or 0)
    return {"costs": totals, "source": "postgres"}


@router.get("/transfer-rate")
async def voice_transfer_rate(
    brand_id: UUID,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    start, end = _date_range(start_date, end_date)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT campaign_id,
                       COUNT(*) AS total_calls,
                       COUNT(*) FILTER (WHERE outcome = 'qualified_transfer') AS transferred
                FROM call_logs
                WHERE brand_id = %s AND deleted_at IS NULL
                  AND created_at >= %s AND created_at <= %s
                GROUP BY campaign_id
                """,
                (str(brand_id), start, end),
            )
            rows = await cur.fetchall()
    result: list[dict[str, Any]] = []
    for cid, total, transferred in rows:
        total_i = int(total)
        transferred_i = int(transferred)
        result.append({
            "campaign_id": str(cid) if cid else "",
            "total_calls": total_i,
            "transferred": transferred_i,
            "transfer_rate": round(transferred_i / total_i, 4) if total_i > 0 else 0,
        })
    return {"campaigns": result, "source": "postgres"}
