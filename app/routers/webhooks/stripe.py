"""Stripe webhook receiver.

Path: ``POST /webhooks/stripe`` (matches the hq-x convention shared by
/webhooks/cal, /webhooks/lob, /webhooks/emailbison, /webhooks/dub,
/webhooks/entri).

Flow per event:

  1. Verify ``Stripe-Signature`` against ``STRIPE_WEBHOOK_SECRET``.
     Stripe expects 200 within ~30s — verification is fast (HMAC).
  2. UPSERT into ``business.stripe_events`` keyed on the event id, so
     redeliveries no-op the archive insert. Returns the row id and
     whether it was newly inserted.
  3. For ``checkout.session.completed``: invoke
     ``proposals_svc.mark_paid_and_instantiate`` which flips proposal
     state, activates the partner contract, and fires Cluster 2 via
     ``ca_svc.instantiate_for_payment``.
  4. Stamp ``processed_at`` (success) or ``processing_error`` (failure).

We **always** return 200 once the signature verifies, even if business
processing fails; otherwise Stripe retries the same event indefinitely
and starves the queue. Failures show up via ``processing_error`` and
the unprocessed-events index for operator follow-up.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg import errors as psycopg_errors
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.services import proposals as proposals_svc
from app.services import stripe_client

logger = logging.getLogger(__name__)

router = APIRouter()

_HANDLED_EVENT_TYPES = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    secret = settings.STRIPE_WEBHOOK_SECRET
    if secret is None:
        # Unconfigured: refuse rather than accept-and-discard so the
        # operator notices during setup.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "stripe_not_configured"},
        )

    raw = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "missing_signature"},
        )
    try:
        stripe_client.verify_webhook_signature(
            payload=raw,
            signature_header=sig_header,
            secret=secret.get_secret_value(),
        )
    except stripe_client.StripeWebhookSignatureError as exc:
        logger.warning("stripe webhook signature failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_signature", "message": str(exc)},
        ) from exc

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_json"},
        ) from exc

    event_id = event.get("id")
    event_type = event.get("type")
    if not event_id or not event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "malformed_event"},
        )

    archive_id, inserted = await _archive_event(event)

    if event_type not in _HANDLED_EVENT_TYPES:
        # Recorded but not actioned. Common: payment_intent.created,
        # account.updated, etc.
        await _stamp_processed(archive_id, error=None)
        return JSONResponse({"received": True, "handled": False})

    try:
        await _handle_checkout_completed(event, archive_id=archive_id)
        await _stamp_processed(archive_id, error=None)
    except Exception as exc:  # broad — webhook MUST 200 for Stripe
        logger.exception("stripe webhook processing failed: %s", exc)
        await _stamp_processed(archive_id, error=str(exc))
        # Still return 200 so Stripe stops retrying. Operator follows
        # up via the unprocessed-events index + processing_error column.
    return JSONResponse({"received": True, "handled": True, "replay": not inserted})


async def _archive_event(event: dict[str, Any]) -> tuple[str, bool]:
    """Idempotent archive write. Returns (id, inserted). On unique-violation
    re-fetches the existing row's id.
    """
    payload_jsonb = Jsonb(event)
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO business.stripe_events
                        (stripe_event_id, event_type, livemode, api_version,
                         payload)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        event["id"],
                        event["type"],
                        bool(event.get("livemode", False)),
                        event.get("api_version"),
                        payload_jsonb,
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()
        return row[0], True
    except psycopg_errors.UniqueViolation:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM business.stripe_events WHERE stripe_event_id = %s",
                    (event["id"],),
                )
                row = await cur.fetchone()
        return row[0], False


async def _handle_checkout_completed(
    event: dict[str, Any], *, archive_id: str
) -> None:
    obj = event.get("data", {}).get("object", {})
    session_id = obj.get("id")
    if not session_id:
        raise ValueError("checkout.session event missing id")

    paid_amount_cents = obj.get("amount_total")
    payment_intent_id = obj.get("payment_intent")
    payment_status = obj.get("payment_status")

    # `complete` covers card; `paid` is also possible. ACH async-success
    # comes via checkout.session.async_payment_succeeded with status='paid'.
    if payment_status not in ("paid", "no_payment_required"):
        logger.info(
            "checkout.session event for %s with payment_status=%s — ignoring",
            session_id, payment_status,
        )
        return

    result = await proposals_svc.mark_paid_and_instantiate(
        stripe_checkout_session_id=session_id,
        paid_amount_cents=int(paid_amount_cents) if paid_amount_cents else 0,
        stripe_payment_intent_id=payment_intent_id,
    )

    # Stamp the proposal id back onto the archive row for forensics.
    proposal_id = result["proposal"]["id"]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.stripe_events
                SET proposal_id = %s
                WHERE id = %s
                """,
                (str(proposal_id), str(archive_id)),
            )
        await conn.commit()


async def _stamp_processed(archive_id: str, *, error: str | None) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.stripe_events
                SET processed_at = NOW(),
                    processing_error = %s
                WHERE id = %s
                """,
                (error, str(archive_id)),
            )
        await conn.commit()


__all__ = ["router"]
