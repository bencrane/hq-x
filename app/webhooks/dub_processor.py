"""Project Dub webhook events into `dmaas_dub_events`.

One row per (dub_event_id). Idempotent: ON CONFLICT DO NOTHING on the
unique index so replays are safe. Returns a status dict — `processed` if
the row was inserted, `duplicate_skipped` if it already existed.

Schema for the three event types we handle:
  - link.clicked → data.{linkId, country, city, device, browser, os, referer}
  - lead.created → data.{linkId, customer:{id,email,…}}
  - sale.created → data.{linkId, customer, sale:{amount, currency, …}}
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection


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
    return {"status": "processed", "dub_link_id": dub_link_id}
