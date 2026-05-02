"""Customer-facing webhook subscription API.

Standard SaaS webhook surface: subscribe, list, update, pause, rotate
secret, view delivery history, replay a specific delivery.

Auth: every endpoint is org-scoped via ``require_org_context``. Cross-org
access returns 404, never 403 (don't leak existence).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.customer_webhooks import (
    CustomerWebhookDeliveryResponse,
    CustomerWebhookSubscriptionCreate,
    CustomerWebhookSubscriptionResponse,
    CustomerWebhookSubscriptionUpdate,
    CustomerWebhookSubscriptionWithSecretResponse,
)
from app.services import customer_webhooks as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dmaas/webhooks", tags=["dmaas-webhooks"])


def _not_found(error: str = "subscription_not_found") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


@router.post(
    "",
    response_model=CustomerWebhookSubscriptionWithSecretResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription_endpoint(
    body: CustomerWebhookSubscriptionCreate,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookSubscriptionWithSecretResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    return await svc.create_subscription(organization_id=org_id, payload=body)


@router.get(
    "",
    response_model=list[CustomerWebhookSubscriptionResponse],
)
async def list_subscriptions_endpoint(
    user: UserContext = Depends(require_org_context),
) -> list[CustomerWebhookSubscriptionResponse]:
    org_id = user.active_organization_id
    assert org_id is not None
    return await svc.list_subscriptions(organization_id=org_id)


@router.get(
    "/{subscription_id}",
    response_model=CustomerWebhookSubscriptionResponse,
)
async def get_subscription_endpoint(
    subscription_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookSubscriptionResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await svc.get_subscription(
            subscription_id=subscription_id, organization_id=org_id
        )
    except svc.SubscriptionNotFound as exc:
        raise _not_found() from exc


@router.patch(
    "/{subscription_id}",
    response_model=CustomerWebhookSubscriptionResponse,
)
async def patch_subscription_endpoint(
    subscription_id: UUID,
    body: CustomerWebhookSubscriptionUpdate,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookSubscriptionResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await svc.update_subscription(
            subscription_id=subscription_id,
            organization_id=org_id,
            payload=body,
        )
    except svc.SubscriptionNotFound as exc:
        raise _not_found() from exc


@router.delete(
    "/{subscription_id}",
    response_model=CustomerWebhookSubscriptionResponse,
)
async def delete_subscription_endpoint(
    subscription_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookSubscriptionResponse:
    """Soft delete: pause the subscription. We never hard-delete to
    preserve the delivery audit trail."""
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await svc.pause_subscription(
            subscription_id=subscription_id, organization_id=org_id
        )
    except svc.SubscriptionNotFound as exc:
        raise _not_found() from exc


@router.post(
    "/{subscription_id}/rotate-secret",
    response_model=CustomerWebhookSubscriptionWithSecretResponse,
)
async def rotate_secret_endpoint(
    subscription_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookSubscriptionWithSecretResponse:
    """Mint a new secret. Old secret stops working immediately. The new
    plaintext is returned exactly once; the customer must save it."""
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        return await svc.rotate_secret(
            subscription_id=subscription_id, organization_id=org_id
        )
    except svc.SubscriptionNotFound as exc:
        raise _not_found() from exc


@router.get(
    "/{subscription_id}/deliveries",
    response_model=list[CustomerWebhookDeliveryResponse],
)
async def list_deliveries_endpoint(
    subscription_id: UUID,
    limit: int = 50,
    user: UserContext = Depends(require_org_context),
) -> list[CustomerWebhookDeliveryResponse]:
    org_id = user.active_organization_id
    assert org_id is not None
    # Verify subscription exists in this org first to surface a clean 404.
    try:
        await svc.get_subscription(
            subscription_id=subscription_id, organization_id=org_id
        )
    except svc.SubscriptionNotFound as exc:
        raise _not_found() from exc
    return await svc.list_deliveries(
        subscription_id=subscription_id,
        organization_id=org_id,
        limit=min(max(int(limit), 1), 200),
    )


@router.post(
    "/{subscription_id}/deliveries/{delivery_id}/retry",
    response_model=CustomerWebhookDeliveryResponse,
)
async def retry_delivery_endpoint(
    subscription_id: UUID,
    delivery_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CustomerWebhookDeliveryResponse:
    """Manually re-fire a dead-lettered delivery. Resets the attempt
    counter and re-enqueues the underlying Trigger.dev task."""
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        delivery = await svc.get_delivery(
            delivery_id=delivery_id, organization_id=org_id
        )
    except svc.DeliveryNotFound as exc:
        raise _not_found("delivery_not_found") from exc
    if delivery.subscription_id != subscription_id:
        raise _not_found("delivery_not_found")

    # Re-arm the row to pending and enqueue a Trigger.dev task to deliver.
    from app.db import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.customer_webhook_deliveries
                SET status = 'pending',
                    attempt = 1,
                    next_retry_at = NULL,
                    response_status = NULL,
                    response_body = NULL,
                    attempted_at = NOW()
                WHERE id = %s
                """,
                (str(delivery_id),),
            )
        await conn.commit()

    from app.services import activation_jobs as jobs_svc

    try:
        await jobs_svc.enqueue_via_trigger(
            job=None,  # type: ignore[arg-type]
            task_identifier="customer_webhook.deliver",
            payload_override={"delivery_id": str(delivery_id)},
        )
    except jobs_svc.TriggerEnqueueError as exc:
        logger.warning(
            "customer_webhook.retry_enqueue_failed",
            extra={"delivery_id": str(delivery_id), "error": str(exc)[:200]},
        )
        # Reconciliation cron will pick this up.

    return await svc.get_delivery(
        delivery_id=delivery_id, organization_id=org_id
    )


__all__ = ["router"]
