"""Re-enqueue pending customer-webhook deliveries past their next_retry_at.

Runs every 15 minutes. Picks up deliveries whose ``next_retry_at`` is
in the past (or null, for the initial dispatch) and enqueues a fresh
Trigger.dev task. The delivery row's attempt counter is unchanged
until the actual delivery attempt records a result.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.config import settings
from app.services import activation_jobs as jobs_svc
from app.services import customer_webhooks as cw_svc
from app.services.reconciliation import ReconciliationResult

logger = logging.getLogger(__name__)


async def reconcile(*, organization_id: UUID | None = None) -> ReconciliationResult:
    if not settings.DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED:
        return ReconciliationResult(enabled=False)

    result = ReconciliationResult()
    pending = await cw_svc.find_pending_due_deliveries(limit=100)
    result.rows_scanned = len(pending)

    for delivery in pending:
        try:
            await jobs_svc.enqueue_via_trigger(
                job=None,
                task_identifier="customer_webhook.deliver",
                payload_override={"delivery_id": str(delivery.id)},
            )
            result.rows_touched += 1
        except jobs_svc.TriggerEnqueueError as exc:
            logger.warning(
                "reconcile.customer_webhooks.enqueue_failed",
                extra={"delivery_id": str(delivery.id), "error": str(exc)[:200]},
            )
            result.add_drift(
                kind="customer_webhook_re_enqueue_failed",
                delivery_id=str(delivery.id),
                error=str(exc)[:200],
            )

    return result


__all__ = ["reconcile"]
