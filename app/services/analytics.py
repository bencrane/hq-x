"""Analytics event helpers that enforce campaign tagging.

Every operational event we ship to Rudderstack and ClickHouse must carry the
six-tuple (organization_id, brand_id, campaign_id, channel_campaign_id,
channel, provider). Routing those through one helper keeps the contract
enforceable in code review — a piece-emit site that doesn't supply a
channel_campaign id won't compile, instead of silently writing an untagged
row.

The Rudderstack write side is intentionally a no-op shim today; we have not
wired the rudder client into hq-x yet (see AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md).
ClickHouse uses ``app.clickhouse.insert_row``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.clickhouse import insert_row
from app.observability.logging import log_event
from app.services.channel_campaigns import get_channel_campaign_context

logger = logging.getLogger(__name__)


class AnalyticsContextMissing(Exception):
    """Raised when an event is emitted without a resolvable campaign context."""


async def resolve_channel_campaign_context(
    channel_campaign_id: UUID,
) -> dict[str, Any]:
    """Resolve the canonical six-tuple for a channel_campaign id.

    Raises ``AnalyticsContextMissing`` if the row cannot be found — callers
    should not be emitting events for non-existent channel campaigns.
    """
    context = await get_channel_campaign_context(
        channel_campaign_id=channel_campaign_id
    )
    if context is None:
        raise AnalyticsContextMissing(
            f"channel_campaign {channel_campaign_id} has no resolvable "
            f"analytics context"
        )
    return context


async def emit_event(
    *,
    event_name: str,
    channel_campaign_id: UUID,
    properties: dict[str, Any] | None = None,
    clickhouse_table: str | None = None,
) -> None:
    """Emit a single analytics event, fully tagged with the campaign tuple.

    `event_name` is the logical event ('direct_mail_piece_created', etc.).
    `properties` carries event-specific attributes. The six-tuple is
    *always* added on top — callers cannot override it.

    `clickhouse_table` is optional. When set, the row is also inserted into
    ClickHouse via the existing fire-and-forget client. Logging always
    happens regardless of ClickHouse configuration so events are visible
    in stdout even before analytics infra is wired.
    """
    context = await resolve_channel_campaign_context(channel_campaign_id)
    payload: dict[str, Any] = {
        **context,
        **(properties or {}),
        "event": event_name,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    log_event(event_name, **payload)

    if clickhouse_table:
        try:
            insert_row(clickhouse_table, payload)
        except Exception:  # pragma: no cover — clickhouse client already swallows
            logger.exception("clickhouse insert raised unexpectedly")


__all__ = [
    "AnalyticsContextMissing",
    "resolve_channel_campaign_context",
    "emit_event",
]
