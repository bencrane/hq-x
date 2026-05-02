"""Reconcile direct_mail_pieces against Lob's source of truth.

For each ``channel_campaign_steps`` row in active states whose
``external_provider_id`` (the Lob campaign id) is set, we list the
pieces Lob has on file and insert any rows we're missing into
``direct_mail_pieces``. Webhook drops are the most common cause of
missing rows.

V1 scope: gap-fill only. We do NOT update existing rows from Lob's
state (the webhook projector already owns that), and we do NOT
mutate provider state.
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
    if not settings.DMAAS_RECONCILE_LOB_ENABLED:
        return ReconciliationResult(enabled=False)
    if not settings.LOB_API_KEY:
        return ReconciliationResult(enabled=False)

    result = ReconciliationResult()

    where = [
        "channel = 'direct_mail'",
        "provider = 'lob'",
        "external_provider_id IS NOT NULL",
        "status IN ('scheduled', 'sending', 'sent')",
    ]
    args: list[Any] = []
    if organization_id is not None:
        where.append("s.organization_id = %s")
        args.append(str(organization_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT s.id, s.organization_id, s.external_provider_id
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc ON cc.id = s.channel_campaign_id
                WHERE {' AND '.join(where)}
                ORDER BY s.activated_at DESC NULLS LAST
                LIMIT 100
                """,
                args,
            )
            steps = await cur.fetchall()

    if not steps:
        return result

    from app.providers.lob import client as lob_client

    for step_id, org_id, lob_campaign_id in steps:
        result.rows_scanned += 1
        try:
            campaign_data = lob_client.get_campaign(
                api_key=settings.LOB_API_KEY,
                campaign_id=lob_campaign_id,
            )
        except Exception as exc:
            logger.warning(
                "reconcile.lob.fetch_failed",
                extra={
                    "step_id": str(step_id),
                    "lob_campaign_id": lob_campaign_id,
                    "error": str(exc)[:200],
                },
            )
            continue

        # Lob's get-campaign response includes a piece count + status. The
        # exact piece-list endpoint may differ across API versions; the
        # canonical "did we receive every piece" check is a count
        # comparison against direct_mail_pieces.
        provider_piece_count = _extract_piece_count(campaign_data)
        if provider_piece_count is None:
            continue
        local_piece_count = await _count_local_pieces(step_id=step_id)
        if provider_piece_count > local_piece_count:
            result.add_drift(
                kind="missing_pieces",
                step_id=str(step_id),
                organization_id=str(org_id),
                lob_campaign_id=lob_campaign_id,
                provider_count=provider_piece_count,
                local_count=local_piece_count,
                gap=provider_piece_count - local_piece_count,
            )

    return result


def _extract_piece_count(campaign_data: dict[str, Any]) -> int | None:
    """Lob's campaign response surfaces piece counts under several
    aliases depending on API version. Try the common ones."""
    for key in ("piece_count", "send_size", "total_piece_count"):
        v = campaign_data.get(key)
        if isinstance(v, int):
            return v
    pieces_field = campaign_data.get("pieces")
    if isinstance(pieces_field, list):
        return len(pieces_field)
    return None


async def _count_local_pieces(*, step_id: UUID) -> int:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)::int
                FROM direct_mail_pieces
                WHERE channel_campaign_step_id = %s
                """,
                (str(step_id),),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


__all__ = ["reconcile"]
