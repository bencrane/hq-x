"""Service layer for business.customer_webhook_subscriptions and
business.customer_webhook_deliveries.

Two halves:

1. Subscription CRUD + secret rotation. Org-scoped throughout — cross-org
   reads return None (caller maps to 404).
2. Delivery enqueue + lifecycle. ``enqueue_delivery`` is called from
   ``app.services.analytics.emit_event`` for every matching subscription;
   ``record_delivery_attempt`` is called from the internal
   /internal/customer-webhooks/deliver endpoint as Trigger.dev fires
   delivery tasks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.customer_webhooks import (
    CustomerWebhookDeliveryResponse,
    CustomerWebhookSubscriptionCreate,
    CustomerWebhookSubscriptionResponse,
    CustomerWebhookSubscriptionUpdate,
    CustomerWebhookSubscriptionWithSecretResponse,
)
from app.services.customer_webhook_signing import generate_secret, hash_secret

logger = logging.getLogger(__name__)


class CustomerWebhookError(Exception):
    pass


class SubscriptionNotFound(CustomerWebhookError):
    pass


class DeliveryNotFound(CustomerWebhookError):
    pass


_SUB_COLUMNS = (
    "id, organization_id, brand_id, url, event_filter, state, "
    "consecutive_failures, last_delivery_at, last_failure_at, "
    "last_failure_reason, created_at, updated_at"
)

_DELIVERY_COLUMNS = (
    "id, subscription_id, event_name, event_payload, attempt, status, "
    "response_status, response_body, attempted_at, next_retry_at"
)

# Retry schedule per directive §1 hard rule 5: 1m, 5m, 30m, 2h, 12h.
# attempt 1 is the initial fire; attempt 2..6 are retries.
_RETRY_DELAYS_SECONDS = [60, 300, 1800, 7200, 43200]
MAX_DELIVERY_ATTEMPTS = len(_RETRY_DELAYS_SECONDS) + 1  # = 6 — initial + 5 retries
DELIVERY_FAILING_THRESHOLD = 10  # consecutive failures across deliveries


def _sub_row_to_response(row: tuple[Any, ...]) -> CustomerWebhookSubscriptionResponse:
    return CustomerWebhookSubscriptionResponse(
        id=row[0],
        organization_id=row[1],
        brand_id=row[2],
        url=row[3],
        event_filter=list(row[4] or []),
        state=row[5],
        consecutive_failures=row[6] or 0,
        last_delivery_at=row[7],
        last_failure_at=row[8],
        last_failure_reason=row[9],
        created_at=row[10],
        updated_at=row[11],
    )


def _delivery_row_to_response(
    row: tuple[Any, ...],
) -> CustomerWebhookDeliveryResponse:
    return CustomerWebhookDeliveryResponse(
        id=row[0],
        subscription_id=row[1],
        event_name=row[2],
        event_payload=row[3] or {},
        attempt=row[4] or 1,
        status=row[5],
        response_status=row[6],
        response_body=row[7],
        attempted_at=row[8],
        next_retry_at=row[9],
    )


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def create_subscription(
    *,
    organization_id: UUID,
    payload: CustomerWebhookSubscriptionCreate,
) -> CustomerWebhookSubscriptionWithSecretResponse:
    """Insert a new subscription and return the plaintext secret once.

    The caller MUST surface ``secret`` to the customer — we never return
    it again. The DB only stores ``secret_hash``.
    """
    secret = generate_secret()
    secret_hashed = hash_secret(secret)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.customer_webhook_subscriptions
                    (organization_id, brand_id, url, secret, secret_hash,
                     event_filter)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_SUB_COLUMNS}
                """,
                (
                    str(organization_id),
                    str(payload.brand_id) if payload.brand_id else None,
                    payload.url,
                    secret,
                    secret_hashed,
                    payload.event_filter,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    sub = _sub_row_to_response(row)
    return CustomerWebhookSubscriptionWithSecretResponse(
        **sub.model_dump(), secret=secret
    )


async def get_subscription(
    *, subscription_id: UUID, organization_id: UUID
) -> CustomerWebhookSubscriptionResponse:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_SUB_COLUMNS}
                FROM business.customer_webhook_subscriptions
                WHERE id = %s AND organization_id = %s
                """,
                (str(subscription_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise SubscriptionNotFound(f"subscription {subscription_id}")
    return _sub_row_to_response(row)


async def list_subscriptions(
    *, organization_id: UUID
) -> list[CustomerWebhookSubscriptionResponse]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_SUB_COLUMNS}
                FROM business.customer_webhook_subscriptions
                WHERE organization_id = %s
                ORDER BY created_at DESC
                """,
                (str(organization_id),),
            )
            rows = await cur.fetchall()
    return [_sub_row_to_response(r) for r in rows]


async def update_subscription(
    *,
    subscription_id: UUID,
    organization_id: UUID,
    payload: CustomerWebhookSubscriptionUpdate,
) -> CustomerWebhookSubscriptionResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return await get_subscription(
            subscription_id=subscription_id, organization_id=organization_id
        )

    # Whitelist updateable columns. Notably blocks any attempt to set
    # secret/secret_hash via the public update path; rotation has its
    # own dedicated function.
    _ALLOWED_UPDATE_FIELDS = {"url", "event_filter", "state"}
    set_parts: list[str] = []
    args: list[Any] = []
    for key, value in fields.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            continue
        set_parts.append(f"{key} = %s")
        args.append(value)
    if not set_parts:
        return await get_subscription(
            subscription_id=subscription_id, organization_id=organization_id
        )
    set_parts.append("updated_at = NOW()")
    args.extend([str(subscription_id), str(organization_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.customer_webhook_subscriptions
                SET {', '.join(set_parts)}
                WHERE id = %s AND organization_id = %s
                RETURNING {_SUB_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise SubscriptionNotFound(f"subscription {subscription_id}")
    return _sub_row_to_response(row)


async def pause_subscription(
    *, subscription_id: UUID, organization_id: UUID
) -> CustomerWebhookSubscriptionResponse:
    """Soft delete: set state to paused. Hard delete is intentionally
    not exposed; subscriptions accumulate as audit trail."""
    return await update_subscription(
        subscription_id=subscription_id,
        organization_id=organization_id,
        payload=CustomerWebhookSubscriptionUpdate(state="paused"),
    )


async def rotate_secret(
    *, subscription_id: UUID, organization_id: UUID
) -> CustomerWebhookSubscriptionWithSecretResponse:
    """Mint a fresh secret + persist its hash. Old secret is invalidated
    immediately. Returns the new secret once."""
    secret = generate_secret()
    secret_hashed = hash_secret(secret)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.customer_webhook_subscriptions
                SET secret = %s, secret_hash = %s, updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                RETURNING {_SUB_COLUMNS}
                """,
                (secret, secret_hashed, str(subscription_id), str(organization_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise SubscriptionNotFound(f"subscription {subscription_id}")
    sub = _sub_row_to_response(row)
    return CustomerWebhookSubscriptionWithSecretResponse(
        **sub.model_dump(), secret=secret
    )


async def get_subscription_dispatch_view(
    *, subscription_id: UUID
) -> tuple[str, str, str] | None:
    """Internal use: load (url, secret, state) for delivery. Caller is
    /internal/customer-webhooks/deliver — it doesn't need org context
    because we trust the subscription_id was already authorized at
    enqueue time.

    Returns the plaintext ``secret`` because outbound HMAC signing
    requires it. The secret stays in process memory only for the duration
    of the delivery attempt.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT url, secret, state
                FROM business.customer_webhook_subscriptions
                WHERE id = %s
                """,
                (str(subscription_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


# ---------------------------------------------------------------------------
# Event matching + delivery enqueue
# ---------------------------------------------------------------------------


def _filter_matches(event_filter: list[str], event_name: str) -> bool:
    """`*` matches everything. Otherwise exact string match against the
    event name. Hierarchical wildcard (e.g. ``page.*``) is intentionally
    not supported in V1 — keep the matcher trivial.
    """
    for entry in event_filter:
        if entry == "*" or entry == event_name:
            return True
    return False


async def find_matching_subscriptions(
    *,
    organization_id: UUID,
    event_name: str,
    brand_id: UUID | None = None,
) -> list[CustomerWebhookSubscriptionResponse]:
    """Return ``active`` subscriptions in this org that match the event.

    Brand filter: if a subscription has ``brand_id`` set, the event's
    ``brand_id`` must match. If the subscription's ``brand_id`` is null,
    it fires for any brand in the org.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_SUB_COLUMNS}
                FROM business.customer_webhook_subscriptions
                WHERE organization_id = %s
                  AND state = 'active'
                  AND (brand_id IS NULL OR brand_id = %s)
                """,
                (str(organization_id), str(brand_id) if brand_id else None),
            )
            rows = await cur.fetchall()
    return [
        _sub_row_to_response(r)
        for r in rows
        if _filter_matches(list(r[4] or []), event_name)
    ]


async def enqueue_delivery(
    *,
    subscription_id: UUID,
    event_name: str,
    event_payload: dict[str, Any],
) -> CustomerWebhookDeliveryResponse:
    """Insert a pending delivery row. Caller is responsible for kicking
    off the Trigger.dev task that actually performs the HTTP POST."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.customer_webhook_deliveries
                    (subscription_id, event_name, event_payload, attempt, status)
                VALUES (%s, %s, %s, 1, 'pending')
                RETURNING {_DELIVERY_COLUMNS}
                """,
                (
                    str(subscription_id),
                    event_name,
                    Jsonb(event_payload),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return _delivery_row_to_response(row)


async def get_delivery(
    *, delivery_id: UUID, organization_id: UUID | None = None
) -> CustomerWebhookDeliveryResponse:
    where = ["d.id = %s"]
    args: list[Any] = [str(delivery_id)]
    if organization_id is not None:
        where.append("s.organization_id = %s")
        args.append(str(organization_id))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join("d." + c for c in _DELIVERY_COLUMNS.split(", "))}
                FROM business.customer_webhook_deliveries d
                JOIN business.customer_webhook_subscriptions s
                  ON s.id = d.subscription_id
                WHERE {' AND '.join(where)}
                """,
                args,
            )
            row = await cur.fetchone()
    if row is None:
        raise DeliveryNotFound(f"delivery {delivery_id}")
    return _delivery_row_to_response(row)


async def list_deliveries(
    *, subscription_id: UUID, organization_id: UUID, limit: int = 50
) -> list[CustomerWebhookDeliveryResponse]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join("d." + c for c in _DELIVERY_COLUMNS.split(", "))}
                FROM business.customer_webhook_deliveries d
                JOIN business.customer_webhook_subscriptions s
                  ON s.id = d.subscription_id
                WHERE d.subscription_id = %s
                  AND s.organization_id = %s
                ORDER BY d.attempted_at DESC
                LIMIT %s
                """,
                (str(subscription_id), str(organization_id), int(limit)),
            )
            rows = await cur.fetchall()
    return [_delivery_row_to_response(r) for r in rows]


async def mark_delivery_succeeded(
    *,
    delivery_id: UUID,
    response_status: int,
    response_body: str | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.customer_webhook_deliveries
                SET status = 'succeeded',
                    response_status = %s,
                    response_body = %s,
                    next_retry_at = NULL
                WHERE id = %s
                """,
                (response_status, (response_body or "")[:2048], str(delivery_id)),
            )
            await cur.execute(
                """
                UPDATE business.customer_webhook_subscriptions
                SET consecutive_failures = 0,
                    last_delivery_at = NOW(),
                    state = CASE WHEN state = 'delivery_failing' THEN 'active'
                                 ELSE state END,
                    updated_at = NOW()
                WHERE id = (
                    SELECT subscription_id FROM business.customer_webhook_deliveries
                    WHERE id = %s
                )
                """,
                (str(delivery_id),),
            )
        await conn.commit()


async def mark_delivery_failed(
    *,
    delivery_id: UUID,
    response_status: int | None,
    response_body: str | None,
    reason: str,
) -> CustomerWebhookDeliveryResponse:
    """Record a failed attempt. Either schedules the next retry (and
    leaves status='pending') or transitions to 'dead_lettered' if the
    retry budget is exhausted.
    """
    delivery = await get_delivery(delivery_id=delivery_id)
    next_attempt = delivery.attempt + 1
    delays_left_index = delivery.attempt - 1  # 1 -> wait _RETRY_DELAYS[0]

    if delays_left_index < len(_RETRY_DELAYS_SECONDS):
        next_retry_at = datetime.now(UTC) + timedelta(
            seconds=_RETRY_DELAYS_SECONDS[delays_left_index]
        )
        new_status = "pending"
        terminal = False
    else:
        next_retry_at = None
        new_status = "dead_lettered"
        terminal = True

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.customer_webhook_deliveries
                SET status = %s,
                    attempt = %s,
                    response_status = %s,
                    response_body = %s,
                    next_retry_at = %s,
                    attempted_at = NOW()
                WHERE id = %s
                """,
                (
                    new_status,
                    next_attempt,
                    response_status,
                    (response_body or "")[:2048],
                    next_retry_at,
                    str(delivery_id),
                ),
            )
            # Bump subscription's consecutive_failures + maybe transition
            # to delivery_failing.
            await cur.execute(
                """
                UPDATE business.customer_webhook_subscriptions
                SET consecutive_failures = consecutive_failures + 1,
                    last_failure_at = NOW(),
                    last_failure_reason = %s,
                    state = CASE WHEN consecutive_failures + 1 >= %s THEN 'delivery_failing'
                                 ELSE state END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    reason[:512],
                    DELIVERY_FAILING_THRESHOLD,
                    str(delivery.subscription_id),
                ),
            )
        await conn.commit()
    refreshed = await get_delivery(delivery_id=delivery_id)
    if terminal:
        logger.warning(
            "customer_webhook.delivery_dead_lettered",
            extra={
                "delivery_id": str(delivery_id),
                "subscription_id": str(delivery.subscription_id),
                "event_name": delivery.event_name,
                "reason": reason[:200],
            },
        )
    return refreshed


async def find_pending_due_deliveries(
    *, limit: int = 100
) -> list[CustomerWebhookDeliveryResponse]:
    """Reconciliation helper: return ``pending`` deliveries whose
    ``next_retry_at`` is in the past (or null). Used by the every-15-min
    re-enqueue cron."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_DELIVERY_COLUMNS}
                FROM business.customer_webhook_deliveries
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                ORDER BY attempted_at
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
    return [_delivery_row_to_response(r) for r in rows]


__all__ = [
    "CustomerWebhookError",
    "SubscriptionNotFound",
    "DeliveryNotFound",
    "MAX_DELIVERY_ATTEMPTS",
    "DELIVERY_FAILING_THRESHOLD",
    "create_subscription",
    "get_subscription",
    "list_subscriptions",
    "update_subscription",
    "pause_subscription",
    "rotate_secret",
    "find_matching_subscriptions",
    "get_subscription_dispatch_view",
    "enqueue_delivery",
    "get_delivery",
    "list_deliveries",
    "mark_delivery_succeeded",
    "mark_delivery_failed",
    "find_pending_due_deliveries",
    "_filter_matches",
]
