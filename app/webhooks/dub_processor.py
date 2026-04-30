"""Project Dub webhook events into `dmaas_dub_events`.

One row per (dub_event_id). Idempotent: ON CONFLICT DO NOTHING on the
unique index so replays are safe. Returns a status dict — `processed` if
the row was inserted, `duplicate_skipped` if it already existed.

Schema for the three event types we handle:
  - link.clicked → data.{linkId, country, city, device, browser, os, referer}
  - lead.created → data.{linkId, customer:{id,email,…}}
  - sale.created → data.{linkId, customer, sale:{amount, currency, …}}

After a row is inserted (status='processed'), we fan out a fully-tagged
analytics event via ``app/services/analytics.emit_event`` so direct-mail
clicks/leads/sales flow into the same six-tuple-tagged pipeline as Lob
piece events. The lookup goes through ``dmaas_dub_links`` to recover
``(channel_campaign_step_id, recipient_id)``; ``emit_event`` resolves the
rest of the six-tuple from the step row. Links not in ``dmaas_dub_links``
(e.g. operator-minted via the bulk routes for non-step purposes) are
treated as unattributed — the dub_event row is still written, no
analytics event is emitted, and a debug line is logged. The emit is
fire-and-forget: a raise from ``emit_event`` never propagates up so the
webhook receiver always returns 202.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.dmaas.dub_links import get_dub_link_by_dub_id
from app.services.analytics import emit_event

logger = logging.getLogger(__name__)


# Map Dub's wire event names to our internal event-name shape. The rest of
# the analytics pipeline namespaces direct-mail provider events by
# ``provider.action`` (e.g. ``lob.piece.delivered``); ``dub.click`` /
# ``dub.lead`` / ``dub.sale`` follows that convention.
_DUB_EVENT_NAME_MAP: dict[str, str] = {
    "link.clicked": "dub.click",
    "lead.created": "dub.lead",
    "sale.created": "dub.sale",
}


def _extract_link_id(data: dict[str, Any]) -> str | None:
    link = data.get("link")
    if isinstance(link, dict):
        link_id = link.get("id") or link.get("linkId")
        if link_id:
            return str(link_id)
    raw = data.get("linkId") or data.get("link_id")
    return str(raw) if raw else None


def _extract_click_fields(data: dict[str, Any]) -> dict[str, Any]:
    click = data.get("click") if isinstance(data.get("click"), dict) else data
    return {
        "click_country": click.get("country"),
        "click_city": click.get("city"),
        "click_device": click.get("device"),
        "click_browser": click.get("browser"),
        "click_os": click.get("os"),
        "click_referer": click.get("referer") or click.get("referrer"),
    }


def _extract_customer_fields(data: dict[str, Any]) -> dict[str, Any]:
    customer = data.get("customer")
    if not isinstance(customer, dict):
        return {"customer_id": None, "customer_email": None}
    return {
        "customer_id": str(customer.get("id")) if customer.get("id") else None,
        "customer_email": customer.get("email"),
    }


def _extract_sale_fields(data: dict[str, Any]) -> dict[str, Any]:
    sale = data.get("sale")
    if not isinstance(sale, dict):
        return {"sale_amount_cents": None, "sale_currency": None}
    amount = sale.get("amount")
    return {
        "sale_amount_cents": int(amount) if amount is not None else None,
        "sale_currency": sale.get("currency"),
    }


async def project_dub_event(
    *,
    payload: dict[str, Any],
    event_id: str,
    event_type: str,
    occurred_at: str,
    webhook_event_id: UUID | None,
) -> dict[str, Any]:
    """Insert into dmaas_dub_events. Idempotent on dub_event_id.

    Returns {"status": "processed"|"duplicate_skipped", "dub_link_id": str|None}.
    """
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    dub_link_id = _extract_link_id(data)

    fields: dict[str, Any] = {
        "click_country": None,
        "click_city": None,
        "click_device": None,
        "click_browser": None,
        "click_os": None,
        "click_referer": None,
        "customer_id": None,
        "customer_email": None,
        "sale_amount_cents": None,
        "sale_currency": None,
    }

    if event_type == "link.clicked":
        fields.update(_extract_click_fields(data))
    elif event_type == "lead.created":
        fields.update(_extract_customer_fields(data))
        # Lead events also carry click context (the click that converted).
        fields.update(_extract_click_fields(data))
    elif event_type == "sale.created":
        fields.update(_extract_customer_fields(data))
        fields.update(_extract_sale_fields(data))
        fields.update(_extract_click_fields(data))

    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dmaas_dub_events
                (dub_event_id, event_type, dub_link_id, occurred_at,
                 click_country, click_city, click_device, click_browser,
                 click_os, click_referer, customer_id, customer_email,
                 sale_amount_cents, sale_currency, payload, webhook_event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dub_event_id) DO NOTHING
            RETURNING id
            """,
            (
                event_id,
                event_type,
                dub_link_id,
                occurred_at,
                fields["click_country"],
                fields["click_city"],
                fields["click_device"],
                fields["click_browser"],
                fields["click_os"],
                fields["click_referer"],
                fields["customer_id"],
                fields["customer_email"],
                fields["sale_amount_cents"],
                fields["sale_currency"],
                Jsonb(data),
                str(webhook_event_id) if webhook_event_id else None,
            ),
        )
        row = await cur.fetchone()

    if row is None:
        return {"status": "duplicate_skipped", "dub_link_id": dub_link_id}

    # Fan out to the six-tuple-tagged analytics pipeline. Only emit on the
    # first projection of a given dub_event_id; duplicate_skipped paths
    # already emitted on the original processing.
    await _emit_dub_event_analytics(
        dub_link_id=dub_link_id,
        event_type=event_type,
        event_id=event_id,
        fields=fields,
    )

    return {"status": "processed", "dub_link_id": dub_link_id}


async def _emit_dub_event_analytics(
    *,
    dub_link_id: str | None,
    event_type: str,
    event_id: str,
    fields: dict[str, Any],
) -> None:
    """Look up the dub_link in ``dmaas_dub_links`` and emit a tagged
    analytics event. Fire-and-forget — never raises into the caller, so a
    flaky emit can't hold up the webhook 202.
    """
    event_name = _DUB_EVENT_NAME_MAP.get(event_type)
    if event_name is None:
        return

    if dub_link_id is None:
        logger.debug(
            "dub_event_unattributed_no_link_id event_type=%s event_id=%s",
            event_type,
            event_id,
        )
        return

    try:
        link = await get_dub_link_by_dub_id(dub_link_id)
    except Exception:
        logger.exception(
            "dub_event_emit_lookup_failed dub_link_id=%s event_id=%s",
            dub_link_id,
            event_id,
        )
        return

    if link is None or link.channel_campaign_step_id is None:
        logger.debug(
            "dub_event_unattributed dub_link_id=%s event_type=%s event_id=%s",
            dub_link_id,
            event_type,
            event_id,
        )
        return

    properties: dict[str, Any] = {
        "dub_link_id": dub_link_id,
        "dub_event_id": event_id,
        "click_url": link.destination_url,
    }
    # Forward the click + customer + sale fields the projector already
    # extracted, dropping Nones to keep the payload tight.
    for key in (
        "click_country",
        "click_city",
        "click_device",
        "click_browser",
        "click_os",
        "click_referer",
        "customer_id",
        "customer_email",
        "sale_amount_cents",
        "sale_currency",
    ):
        value = fields.get(key)
        if value is not None:
            properties[key] = value

    try:
        await emit_event(
            event_name=event_name,
            channel_campaign_step_id=link.channel_campaign_step_id,
            recipient_id=link.recipient_id,
            properties=properties,
        )
    except Exception:
        logger.exception(
            "dub_event_emit_failed dub_link_id=%s event_type=%s event_id=%s",
            dub_link_id,
            event_type,
            event_id,
        )
