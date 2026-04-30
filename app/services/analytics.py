"""Analytics event helpers that enforce campaign tagging.

Every operational event we ship to RudderStack and ClickHouse must carry
the six-tuple
``(organization_id, brand_id, campaign_id, channel_campaign_id,
   channel_campaign_step_id, channel, provider)``.
Routing those through one helper keeps the contract enforceable in code
review — a piece-emit site that doesn't supply either a channel_campaign
id or a step id won't compile, instead of silently writing an untagged
row.

Resolution order: if the caller supplies a ``channel_campaign_step_id``
we resolve everything (including the step id itself) from the step row.
If the caller only has a ``channel_campaign_id``, we fall back to that —
``channel_campaign_step_id`` will be omitted from the emitted payload.

Fan-out order: log first, ClickHouse second (no-op without a configured
cluster — currently the perpetual state), RudderStack third. Each call
is wrapped in its own try/except; nothing re-raises into the caller.
The RudderStack hook uses ``organization_id`` as ``anonymous_id`` since
hq-x has only platform-operator users today; the per-recipient
``recipient_id`` rides along inside the event ``properties`` payload.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app import rudderstack
from app.clickhouse import insert_row
from app.observability.logging import log_event
from app.services.channel_campaign_steps import get_step_context
from app.services.channel_campaigns import get_channel_campaign_context

logger = logging.getLogger(__name__)


class AnalyticsContextMissing(Exception):
    """Raised when an event is emitted without a resolvable campaign context."""


async def resolve_channel_campaign_context(
    channel_campaign_id: UUID,
) -> dict[str, Any]:
    """Resolve the canonical tuple for a channel_campaign id.

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


async def resolve_step_context(step_id: UUID) -> dict[str, Any]:
    """Resolve the full six-tuple for a channel_campaign_step id.

    Raises ``AnalyticsContextMissing`` if the step is not found.
    """
    context = await get_step_context(step_id=step_id)
    if context is None:
        raise AnalyticsContextMissing(
            f"channel_campaign_step {step_id} has no resolvable analytics context"
        )
    return context


async def emit_event(
    *,
    event_name: str,
    channel_campaign_step_id: UUID | None = None,
    channel_campaign_id: UUID | None = None,
    recipient_id: UUID | None = None,
    properties: dict[str, Any] | None = None,
    clickhouse_table: str | None = None,
) -> None:
    """Emit a single analytics event, fully tagged with the campaign tuple.

    Caller must supply at least one of ``channel_campaign_step_id`` or
    ``channel_campaign_id``. Step is preferred; we resolve up the chain
    from there so events carry the full step → channel_campaign →
    campaign → brand → org context.

    The six-tuple is *always* added on top of ``properties`` — callers
    cannot override it.

    `clickhouse_table` is optional. When set, the row is also inserted
    into ClickHouse via the existing fire-and-forget client. Logging
    always happens regardless of ClickHouse configuration so events are
    visible in stdout even before analytics infra is wired.
    """
    if channel_campaign_step_id is not None:
        context = await resolve_step_context(channel_campaign_step_id)
    elif channel_campaign_id is not None:
        context = await resolve_channel_campaign_context(channel_campaign_id)
    else:
        raise AnalyticsContextMissing(
            "emit_event requires channel_campaign_step_id or channel_campaign_id"
        )

    payload: dict[str, Any] = {
        **context,
        **(properties or {}),
        "event": event_name,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if recipient_id is not None:
        payload["recipient_id"] = str(recipient_id)
    # log_event takes ``event`` positionally; passing it again via
    # **payload would be a TypeError, so build the kw dict without it.
    log_fields = {k: v for k, v in payload.items() if k != "event"}
    log_event(event_name, **log_fields)

    if clickhouse_table:
        try:
            insert_row(clickhouse_table, payload)
        except Exception:  # pragma: no cover — clickhouse client already swallows
            logger.exception("clickhouse insert raised unexpectedly")

    # RudderStack fan-out. Anonymous_id is the organization_id (hq-x has
    # only platform-operator users today); the full payload — six-tuple,
    # recipient_id, occurred_at, plus caller-supplied extras — rides as
    # ``properties``. Fire-and-forget, never re-raises.
    try:
        org_id_str = context.get("organization_id")
        if org_id_str:
            rudderstack.track(
                event_name=event_name,
                anonymous_id=str(org_id_str),
                properties=payload,
            )
    except Exception:  # pragma: no cover — rudderstack.track also swallows
        logger.exception("rudderstack track raised unexpectedly")


__all__ = [
    "AnalyticsContextMissing",
    "resolve_channel_campaign_context",
    "resolve_step_context",
    "emit_event",
]
