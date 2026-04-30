"""Replay non-terminal webhook_events rows.

The Lob/Vapi/Dub/Entri webhook receivers persist every inbound event
to ``webhook_events`` with status `pending` then `processed` /
`dead_letter`. If the projector failed mid-flight the row sits in
non-terminal status. This reconciler picks up rows older than 1 hour
in such states and fires the existing replay machinery.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.services.reconciliation import ReconciliationResult

logger = logging.getLogger(__name__)

# Statuses that indicate a row hasn't reached a terminal state.
_NON_TERMINAL_STATUSES = ("pending", "received", "in_progress")
_TERMINAL_STATUSES = ("processed", "dead_letter")


async def reconcile(*, organization_id: UUID | None = None) -> ReconciliationResult:
    if not settings.DMAAS_RECONCILE_WEBHOOK_REPLAYS_ENABLED:
        return ReconciliationResult(enabled=False)

    result = ReconciliationResult()

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, provider_slug, event_key, status, created_at
                FROM webhook_events
                WHERE created_at < NOW() - INTERVAL '1 hour'
                  AND status NOT IN ('processed', 'dead_letter')
                ORDER BY created_at
                LIMIT 100
                """,
            )
            rows = await cur.fetchall()

    for event_id, provider_slug, event_key, current_status, created_at in rows:
        result.rows_scanned += 1
        # Mark as drift even if we can't replay (e.g. provider doesn't
        # support replay). Operators want to see the existence of these
        # rows.
        result.add_drift(
            kind="non_terminal_webhook_event",
            event_id=str(event_id),
            provider=provider_slug,
            event_key=event_key,
            status=current_status,
            created_at=created_at.isoformat() if created_at is not None else None,
        )

        # For Lob the existing admin replay endpoint at
        # /webhooks/lob/admin/replay/{id} handles the heavy lifting.
        # Calling it from a cron requires server-internal credentials,
        # so V1 we emit drift and let the operator manually replay
        # (or extend a future internal-replay surface). Other providers
        # are scaffolded analogously.
        # The V1 reconciler is intentionally read-only here — we
        # surface drift but don't auto-replay.

    return result


__all__ = ["reconcile"]
