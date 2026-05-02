"""Reliability analytics — webhook ingestion health, per provider.

Reads ``webhook_events`` (the inbound-webhook journal) and rolls it up
by ``provider_slug`` for an org-scoped time window. Surfaces three
operator-facing signals per provider:

* total events received in the window
* total replays attempted (sum of ``replay_count``)
* event count broken down by terminal status (``received``, ``processed``,
  plus any other status the upstream processor wrote)

``webhook_events`` is brand-scoped, not org-scoped; org filtering goes
through ``business.brands.organization_id``. Rows whose ``brand_id`` is
NULL (legacy events from before the brand-axis migration, or events
that arrived before the receiver could resolve a brand) are excluded
from org-scoped queries by definition — they belong to no org.

This is Postgres-only. ClickHouse is irrelevant here: ``webhook_events``
is a relational journal of low cardinality (a few thousand events/day at
hq-x scale), not an event stream.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


async def summarize_reliability(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Aggregate webhook_events for the org over [start, end].

    Returns a dict shaped like::

        {
            "window": {"from": "...", "to": "..."},
            "providers": [
                {
                    "provider_slug": "lob",
                    "events_total": 123,
                    "replays_total": 4,
                    "by_status": {"processed": 120, "received": 3},
                },
                ...
            ],
            "totals": {"events": N, "replays": N},
            "source": "postgres",
        }
    """
    where = [
        "we.brand_id IS NOT NULL",
        "b.organization_id = %s",
        "we.created_at >= %s",
        "we.created_at < %s",
    ]
    values: list[Any] = [str(organization_id), start, end]
    if brand_id is not None:
        where.append("we.brand_id = %s")
        values.append(str(brand_id))

    sql = f"""
        SELECT we.provider_slug,
               we.status,
               COUNT(*) AS event_cnt,
               COALESCE(SUM(we.replay_count), 0) AS replay_cnt
        FROM webhook_events we
        JOIN business.brands b ON b.id = we.brand_id
        WHERE {" AND ".join(where)}
        GROUP BY we.provider_slug, we.status
        ORDER BY we.provider_slug, we.status
    """

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, values)
            rows = await cur.fetchall()

    providers: dict[str, dict[str, Any]] = {}
    events_total = 0
    replays_total = 0
    for provider_slug, status_, event_cnt, replay_cnt in rows:
        bucket = providers.setdefault(
            provider_slug,
            {
                "provider_slug": provider_slug,
                "events_total": 0,
                "replays_total": 0,
                "by_status": {},
            },
        )
        cnt = int(event_cnt)
        replays = int(replay_cnt)
        bucket["events_total"] += cnt
        bucket["replays_total"] += replays
        bucket["by_status"][status_] = cnt
        events_total += cnt
        replays_total += replays

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "providers": list(providers.values()),
        "totals": {"events": events_total, "replays": replays_total},
        "source": "postgres",
    }


__all__ = ["summarize_reliability"]
