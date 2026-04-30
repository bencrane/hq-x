"""Reconcile Dub click counts against our dmaas_dub_events log.

V1 only detects drift: for each dmaas_dub_links row active in the last
24 hours, fetch the link's analytics from Dub and compare ``clicks``
to our locally-recorded count. Surface gaps as drift events but
don't reconstruct missing events (Dub's analytics endpoint is
aggregated; we'd need timeseries to faithfully replay).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.services.reconciliation import ReconciliationResult

logger = logging.getLogger(__name__)


async def reconcile(*, organization_id: UUID | None = None) -> ReconciliationResult:
    if not settings.DMAAS_RECONCILE_DUB_ENABLED:
        return ReconciliationResult(enabled=False)
    if not settings.DUB_API_KEY:
        return ReconciliationResult(enabled=False)

    result = ReconciliationResult()

    where = ["dl.created_at > NOW() - INTERVAL '24 hours'"]
    args: list[Any] = []
    if organization_id is not None:
        where.append("dl.organization_id = %s")
        args.append(str(organization_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT dl.id, dl.organization_id, dl.link_id
                FROM dmaas_dub_links dl
                WHERE {' AND '.join(where)}
                ORDER BY dl.created_at DESC
                LIMIT 200
                """,
                args,
            )
            links = await cur.fetchall()

    if not links:
        return result

    from app.providers.dub import client as dub_client

    api_key = settings.DUB_API_KEY.get_secret_value()
    base_url = settings.DUB_API_BASE_URL

    for link_row, org_id, dub_link_id in links:
        result.rows_scanned += 1
        try:
            data = dub_client.get_link(
                api_key=api_key, link_id=dub_link_id, base_url=base_url
            )
        except Exception as exc:
            logger.warning(
                "reconcile.dub.fetch_failed",
                extra={"dub_link_id": dub_link_id, "error": str(exc)[:200]},
            )
            continue

        provider_clicks = int(data.get("clicks", 0) or 0)
        local_clicks = await _count_local_click_events(dmaas_dub_link_id=link_row)
        if provider_clicks > local_clicks:
            result.add_drift(
                kind="dub_click_drift",
                organization_id=str(org_id),
                dub_link_id=dub_link_id,
                provider_clicks=provider_clicks,
                local_clicks=local_clicks,
                gap=provider_clicks - local_clicks,
            )

    return result


async def _count_local_click_events(*, dmaas_dub_link_id: UUID) -> int:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)::int
                FROM dmaas_dub_events
                WHERE dmaas_dub_link_id = %s
                  AND event_type = 'click'
                """,
                (str(dmaas_dub_link_id),),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


__all__ = ["reconcile"]
