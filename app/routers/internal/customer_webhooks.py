"""Internal endpoint that performs one customer-webhook delivery attempt.

The Trigger.dev task ``customer_webhook.deliver`` calls this with
``{delivery_id}``. We load the delivery + subscription, compute the
HMAC signature over the raw body, POST to the customer's URL, and
record the result via ``mark_delivery_succeeded`` /
``mark_delivery_failed``.

We do NOT re-raise on HTTP failures — the next retry is scheduled in
the delivery row, and the every-15-min reconciliation cron picks up
``pending`` deliveries past their ``next_retry_at``. Trigger.dev's
task-level retry remains as a backstop for transient hq-x bugs.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.services import customer_webhooks as cw_svc
from app.services.customer_webhook_signing import (
    _SIGNATURE_HEADER,
    sign_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customer-webhooks", tags=["internal"])

_DELIVERY_HTTP_TIMEOUT_SECONDS = 10


async def _http_post(
    *, url: str, body: bytes, headers: dict[str, str]
) -> tuple[int, str]:
    """Single-shot outbound POST. Extracted so tests can monkeypatch
    without subclassing the entire httpx client."""
    async with httpx.AsyncClient(timeout=_DELIVERY_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, content=body, headers=headers)
    return resp.status_code, resp.text[:2048]


@router.post(
    "/deliver",
    dependencies=[Depends(verify_trigger_secret)],
)
async def deliver(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    delivery_id_raw = body.get("delivery_id")
    if delivery_id_raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "missing_delivery_id"},
        )
    delivery_id = UUID(str(delivery_id_raw))

    try:
        delivery = await cw_svc.get_delivery(delivery_id=delivery_id)
    except cw_svc.DeliveryNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "delivery_not_found"},
        ) from exc

    # Idempotent on terminal: don't re-fire a succeeded / dead-lettered row.
    if delivery.status in ("succeeded", "dead_lettered"):
        return {
            "delivery_id": str(delivery_id),
            "status": delivery.status,
            "skipped": True,
        }

    dispatch = await cw_svc.get_subscription_dispatch_view(
        subscription_id=delivery.subscription_id
    )
    if dispatch is None:
        await cw_svc.mark_delivery_failed(
            delivery_id=delivery_id,
            response_status=None,
            response_body=None,
            reason="subscription_not_found",
        )
        return {
            "delivery_id": str(delivery_id),
            "status": "failed",
            "reason": "subscription_not_found",
        }

    url, secret, sub_state = dispatch
    if sub_state == "paused":
        # Cancel the delivery — the customer paused mid-flight.
        await cw_svc.mark_delivery_failed(
            delivery_id=delivery_id,
            response_status=None,
            response_body=None,
            reason="subscription_paused",
        )
        return {
            "delivery_id": str(delivery_id),
            "status": "failed",
            "reason": "subscription_paused",
        }

    envelope = {
        "id": f"wh_evt_{delivery_id}",
        "subscription_id": str(delivery.subscription_id),
        "event": delivery.event_name,
        "occurred_at": delivery.attempted_at.isoformat(),
        "organization_id": delivery.event_payload.get("organization_id"),
        "data": delivery.event_payload,
    }
    raw_body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = sign_payload(secret, raw_body)

    response_status: int | None = None
    response_body: str | None = None
    failure_reason: str | None = None
    try:
        response_status, response_body = await _http_post(
            url=url,
            body=raw_body,
            headers={
                "Content-Type": "application/json",
                _SIGNATURE_HEADER: signature,
                "User-Agent": "hq-x-webhooks/1",
            },
        )
        if 200 <= response_status < 300:
            await cw_svc.mark_delivery_succeeded(
                delivery_id=delivery_id,
                response_status=response_status,
                response_body=response_body,
            )
            return {
                "delivery_id": str(delivery_id),
                "status": "succeeded",
                "response_status": response_status,
            }
        failure_reason = f"http_{response_status}"
    except httpx.TimeoutException:
        failure_reason = "timeout"
    except httpx.RequestError as exc:
        failure_reason = f"request_error: {exc.__class__.__name__}"
        response_body = str(exc)[:2048]

    refreshed = await cw_svc.mark_delivery_failed(
        delivery_id=delivery_id,
        response_status=response_status,
        response_body=response_body,
        reason=failure_reason or "unknown",
    )
    return {
        "delivery_id": str(delivery_id),
        "status": refreshed.status,
        "reason": failure_reason,
    }


__all__ = ["router"]
