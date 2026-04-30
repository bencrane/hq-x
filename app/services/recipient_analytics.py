"""Recipient timeline analytics — the recipient's view of their own
touchpoints with the org.

Given a recipient_id, returns a time-ordered timeline of every
analytics-relevant event we have for that recipient across all
channels and channel_campaigns. The recipient is strictly org-scoped
(``business.recipients.organization_id``) and the lookup uses a single
WHERE clause that combines ``id`` AND ``organization_id`` so a caller
in org A cannot probe recipient ids in org B via timing.

What's surfaced today:

* The recipient row itself (display_name, external source/id,
  recipient_type, created_at).
* Direct-mail piece events: every row in ``direct_mail_piece_events``
  for any ``direct_mail_pieces`` row tagged with this recipient.
* Membership transitions: every ``channel_campaign_step_recipients``
  row for this recipient, surfaced as synthetic
  ``membership.{status}`` events with ``occurred_at = processed_at``
  (or ``created_at`` if processed_at is null).
* Dub link events: ``dmaas_dub_events`` (clicks / leads / sales) for
  any ``dmaas_dub_links`` row bound to this recipient via a
  channel_campaign_step. Surfaces as ``provider=dub`` events under the
  same ``direct_mail`` channel as the piece they're attributed to —
  "they got the postcard AND scanned it" is the headline conversion
  story for the per-recipient view.

What's deferred (per directive §1.4):

* Voice (``call_logs``) and SMS (``sms_messages``) per-recipient
  events — those tables don't carry ``recipient_id`` yet. They
  appear once the upstream wiring lands; until then,
  ``summary.by_channel`` shows zero for those channels.

Pagination: ``limit`` defaults to 100, capped at 500.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


class RecipientNotFound(Exception):
    """Raised when the recipient does not belong to the caller's org."""


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


async def _load_recipient(
    *, organization_id: UUID, recipient_id: UUID
) -> dict[str, Any]:
    """Fetch the recipient row, scoped to org in a single WHERE clause."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, organization_id, recipient_type, external_source,
                       external_id, display_name, created_at
                FROM business.recipients
                WHERE id = %s AND organization_id = %s
                  AND deleted_at IS NULL
                """,
                (str(recipient_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise RecipientNotFound(
            f"recipient {recipient_id} not found in organization "
            f"{organization_id}"
        )
    return {
        "id": str(row[0]),
        "organization_id": str(row[1]),
        "recipient_type": row[2],
        "external_source": row[3],
        "external_id": row[4],
        "display_name": row[5],
        "created_at": row[6].isoformat() if row[6] is not None else None,
    }


async def _load_dm_events(
    *,
    organization_id: UUID,
    recipient_id: UUID,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """All direct_mail_piece_events for the recipient over the window.

    The join binds ``direct_mail_pieces.recipient_id = %s`` and a
    secondary org-isolation guard goes through
    ``business.channel_campaign_steps.organization_id`` (since
    direct_mail_pieces itself doesn't carry organization_id today).
    For pieces with no step (legacy ad-hoc operator sends), we still
    require the brand to be in the caller's org via
    ``business.brands.organization_id``.
    """
    sql = """
        SELECT e.occurred_at, e.event_type, p.id AS piece_id,
               p.campaign_id, p.channel_campaign_id, p.channel_campaign_step_id
        FROM direct_mail_piece_events e
        JOIN direct_mail_pieces p ON p.id = e.piece_id
        LEFT JOIN business.channel_campaign_steps s
          ON s.id = p.channel_campaign_step_id
        LEFT JOIN business.brands b
          ON b.id = p.brand_id
        WHERE p.recipient_id = %s
          AND p.deleted_at IS NULL
          AND e.received_at >= %s
          AND e.received_at < %s
          AND (
              s.organization_id = %s
              OR (s.id IS NULL AND b.organization_id = %s)
          )
        ORDER BY e.occurred_at DESC
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    str(recipient_id),
                    start,
                    end,
                    str(organization_id),
                    str(organization_id),
                ),
            )
            rows = await cur.fetchall()
    return [
        {
            "occurred_at": (
                occurred_at.isoformat() if occurred_at is not None else None
            ),
            "channel": "direct_mail",
            "provider": "lob",
            "event_type": event_type,
            "campaign_id": str(campaign_id) if campaign_id else None,
            "channel_campaign_id": (
                str(channel_campaign_id) if channel_campaign_id else None
            ),
            "channel_campaign_step_id": (
                str(step_id) if step_id else None
            ),
            "artifact_id": str(piece_id),
            "artifact_kind": "direct_mail_piece",
            "metadata": {},
        }
        for occurred_at, event_type, piece_id, campaign_id, channel_campaign_id, step_id in rows  # noqa: E501
    ]


async def _load_membership_events(
    *,
    organization_id: UUID,
    recipient_id: UUID,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Step-membership rows for the recipient, surfaced as synthetic events.

    Each row becomes one ``membership.{status}`` event with
    ``occurred_at = processed_at`` (or ``created_at`` if processed_at is
    null). The step join also adds the channel/provider context so
    consumers can group the event by channel.
    """
    sql = """
        SELECT COALESCE(scr.processed_at, scr.created_at) AS occurred_at,
               scr.status,
               scr.channel_campaign_step_id,
               s.channel_campaign_id,
               s.campaign_id,
               cc.channel,
               cc.provider
        FROM business.channel_campaign_step_recipients scr
        JOIN business.channel_campaign_steps s
          ON s.id = scr.channel_campaign_step_id
        JOIN business.channel_campaigns cc
          ON cc.id = s.channel_campaign_id
        WHERE scr.recipient_id = %s
          AND scr.organization_id = %s
          AND COALESCE(scr.processed_at, scr.created_at) >= %s
          AND COALESCE(scr.processed_at, scr.created_at) < %s
        ORDER BY occurred_at DESC
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    str(recipient_id),
                    str(organization_id),
                    start,
                    end,
                ),
            )
            rows = await cur.fetchall()
    return [
        {
            "occurred_at": (
                occurred_at.isoformat() if occurred_at is not None else None
            ),
            "channel": channel,
            "provider": provider,
            "event_type": f"membership.{status_}",
            "campaign_id": str(campaign_id) if campaign_id else None,
            "channel_campaign_id": (
                str(channel_campaign_id) if channel_campaign_id else None
            ),
            "channel_campaign_step_id": str(step_id) if step_id else None,
            "artifact_id": None,
            "artifact_kind": "step_membership",
            "metadata": {"membership_status": status_},
        }
        for occurred_at, status_, step_id, channel_campaign_id, campaign_id, channel, provider in rows  # noqa: E501
    ]


# Map dmaas_dub_events.event_type → the recipient-timeline event_type we
# expose. Matches the namespacing used by Slice 2's emit_event() fan-out
# so both surfaces speak the same vocabulary.
_DUB_EVENT_NAME_MAP: dict[str, str] = {
    "link.clicked": "dub.click",
    "lead.created": "dub.lead",
    "sale.created": "dub.sale",
}


async def _load_dub_events(
    *,
    organization_id: UUID,
    recipient_id: UUID,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Dub link events for the recipient, joined through dmaas_dub_links
    onto channel_campaign_steps for org isolation.

    Org isolation goes through ``s.organization_id`` (the step the link
    is bound to). Links not bound to a step (e.g. operator-minted via
    bulk routes for an ad-hoc purpose) are intentionally excluded; the
    timeline can't attribute them to a campaign hierarchy.
    """
    sql = """
        SELECT de.occurred_at, de.event_type, de.dub_link_id,
               dl.destination_url,
               de.click_country, de.click_city, de.click_device,
               de.click_browser, de.click_os, de.click_referer,
               de.customer_id, de.customer_email,
               de.sale_amount_cents, de.sale_currency,
               s.id AS step_id, s.channel_campaign_id, s.campaign_id
        FROM dmaas_dub_events de
        JOIN dmaas_dub_links dl ON dl.dub_link_id = de.dub_link_id
        JOIN business.channel_campaign_steps s
          ON s.id = dl.channel_campaign_step_id
        WHERE dl.recipient_id = %s
          AND s.organization_id = %s
          AND de.occurred_at >= %s
          AND de.occurred_at < %s
        ORDER BY de.occurred_at DESC
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    str(recipient_id),
                    str(organization_id),
                    start,
                    end,
                ),
            )
            rows = await cur.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        (
            occurred_at,
            event_type_raw,
            dub_link_id,
            destination_url,
            click_country,
            click_city,
            click_device,
            click_browser,
            click_os,
            click_referer,
            customer_id,
            customer_email,
            sale_amount_cents,
            sale_currency,
            step_id,
            channel_campaign_id,
            campaign_id,
        ) = row
        event_name = _DUB_EVENT_NAME_MAP.get(event_type_raw)
        if event_name is None:
            # Defensive: dmaas_dub_events is constrained to the three
            # known event types upstream; an unmapped row is skipped.
            continue
        metadata: dict[str, Any] = {
            "click_url": destination_url,
        }
        for key, value in (
            ("click_country", click_country),
            ("click_city", click_city),
            ("click_device", click_device),
            ("click_browser", click_browser),
            ("click_os", click_os),
            ("click_referer", click_referer),
            ("customer_id", customer_id),
            ("customer_email", customer_email),
            ("sale_amount_cents", sale_amount_cents),
            ("sale_currency", sale_currency),
        ):
            if value is not None:
                metadata[key] = value
        out.append(
            {
                "occurred_at": (
                    occurred_at.isoformat() if occurred_at is not None else None
                ),
                "channel": "direct_mail",
                "provider": "dub",
                "event_type": event_name,
                "campaign_id": str(campaign_id) if campaign_id else None,
                "channel_campaign_id": (
                    str(channel_campaign_id) if channel_campaign_id else None
                ),
                "channel_campaign_step_id": (
                    str(step_id) if step_id else None
                ),
                "artifact_id": dub_link_id,
                "artifact_kind": "dub_link",
                "metadata": metadata,
            }
        )
    return out


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_channel: dict[str, int] = {
        "direct_mail": 0,
        "voice_outbound": 0,
        "sms": 0,
    }
    campaigns: set[str] = set()
    channel_campaigns: set[str] = set()
    for ev in events:
        ch = ev["channel"]
        if ch in by_channel:
            by_channel[ch] += 1
        else:
            by_channel[ch] = by_channel.get(ch, 0) + 1
        if ev.get("campaign_id"):
            campaigns.add(ev["campaign_id"])
        if ev.get("channel_campaign_id"):
            channel_campaigns.add(ev["channel_campaign_id"])
    return {
        "total_events": len(events),
        "by_channel": by_channel,
        "campaigns_touched": len(campaigns),
        "channel_campaigns_touched": len(channel_campaigns),
    }


async def recipient_timeline(
    *,
    organization_id: UUID,
    recipient_id: UUID,
    start: datetime,
    end: datetime,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Build the recipient timeline payload.

    Raises :class:`RecipientNotFound` when the recipient is not in the
    caller's org. The router maps this to 404; never leak existence.
    """
    if limit < 1:
        limit = 1
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT
    if offset < 0:
        offset = 0

    recipient = await _load_recipient(
        organization_id=organization_id, recipient_id=recipient_id
    )
    dm_events = await _load_dm_events(
        organization_id=organization_id,
        recipient_id=recipient_id,
        start=start,
        end=end,
    )
    membership_events = await _load_membership_events(
        organization_id=organization_id,
        recipient_id=recipient_id,
        start=start,
        end=end,
    )
    dub_events = await _load_dub_events(
        organization_id=organization_id,
        recipient_id=recipient_id,
        start=start,
        end=end,
    )

    all_events = dm_events + membership_events + dub_events
    # occurred_at is an ISO-8601 string; lexical sort matches chronological
    # order for fixed-format UTC timestamps. Fall back to "" for None to
    # keep the sort stable.
    all_events.sort(key=lambda ev: ev["occurred_at"] or "", reverse=True)

    total = len(all_events)
    page = all_events[offset : offset + limit]
    summary = _summarize(all_events)

    return {
        "recipient": recipient,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "summary": summary,
        "events": page,
        "pagination": {"limit": limit, "offset": offset, "total": total},
        "source": "postgres",
    }


__all__ = [
    "RecipientNotFound",
    "recipient_timeline",
]
