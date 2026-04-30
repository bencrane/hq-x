"""Per-step analytics — drilldown for a single channel_campaign_step.

Surfaces the membership funnel
(``pending → scheduled → sent / failed / suppressed / cancelled``),
event totals broken down by ``direct_mail_piece_events.event_type``,
outcomes (succeeded / failed / skipped), the per-piece status funnel,
and total cost in cents.

Voice/SMS steps don't exist as real ``business.channel_campaign_steps``
rows today — the synthetic-step fallback is surfaced at the
channel_campaign rollup layer (see ``campaign_analytics``). Callers
hitting ``/channel-campaign-steps/{id}/summary`` for a synthetic /
non-existent step id receive 404; the org-scoped SELECT enforces this.

Postgres-only; payload always carries ``"source": "postgres"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


class StepNotFound(Exception):
    """Raised when the step does not exist in the caller's organization."""


_DM_OUTCOME_CASE = """
    CASE
        WHEN p.status IN (
            'delivered', 'mailed', 'in_transit',
            'processed_for_delivery', 'in_local_area'
        ) THEN 'succeeded'
        WHEN p.status IN ('failed', 'returned', 'rejected')
            THEN 'failed'
        ELSE 'skipped'
    END
"""

_OUTCOME_KEYS = ("succeeded", "failed", "skipped")
_MEMBERSHIP_KEYS = (
    "pending",
    "scheduled",
    "sent",
    "failed",
    "suppressed",
    "cancelled",
)
_PIECE_FUNNEL_KEYS = (
    "queued",
    "processed",
    "in_transit",
    "delivered",
    "returned",
    "failed",
)


def _empty(keys: tuple[str, ...]) -> dict[str, int]:
    return {k: 0 for k in keys}


# Map raw direct_mail_pieces.status → piece-funnel bucket. Statuses not in
# the map are silently ignored (they don't fit the funnel).
_PIECE_STATUS_TO_FUNNEL = {
    # initial states
    "queued": "queued",
    "processing": "queued",
    "created": "queued",
    "unknown": "queued",
    # processed
    "processed": "processed",
    "ready_for_mail": "processed",
    "mailed": "processed",
    # in transit
    "in_transit": "in_transit",
    "in_local_area": "in_transit",
    "processed_for_delivery": "in_transit",
    # terminal
    "delivered": "delivered",
    "returned": "returned",
    "failed": "failed",
    "rejected": "failed",
}


async def _load_step(
    *, organization_id: UUID, step_id: UUID
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.channel_campaign_id, s.campaign_id,
                       s.step_order, s.name, cc.channel, cc.provider,
                       s.external_provider_id, s.status,
                       s.scheduled_send_at, s.activated_at
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.id = %s AND s.organization_id = %s
                """,
                (str(step_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise StepNotFound(
            f"step {step_id} not found in organization {organization_id}"
        )
    return {
        "id": str(row[0]),
        "channel_campaign_id": str(row[1]),
        "campaign_id": str(row[2]),
        "step_order": row[3],
        "name": row[4],
        "channel": row[5],
        "provider": row[6],
        "external_provider_id": row[7],
        "status": row[8],
        "scheduled_send_at": row[9].isoformat() if row[9] is not None else None,
        "activated_at": row[10].isoformat() if row[10] is not None else None,
    }


async def _load_memberships(
    *, organization_id: UUID, step_id: UUID
) -> dict[str, int]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, COUNT(*)
                FROM business.channel_campaign_step_recipients
                WHERE channel_campaign_step_id = %s
                  AND organization_id = %s
                GROUP BY status
                """,
                (str(step_id), str(organization_id)),
            )
            rows = await cur.fetchall()
    out = _empty(_MEMBERSHIP_KEYS)
    for status_, count in rows:
        out[status_] = int(count)
    return out


async def _load_dm_aggregates(
    *,
    organization_id: UUID,
    step_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Aggregate direct_mail_pieces for the step over the window.

    Returns ``{"events_total", "cost_total_cents", "outcomes",
    "piece_funnel"}``. Outcomes computed in SQL via CASE; piece funnel
    composed in Python from raw ``status`` rows (small cardinality).
    """
    sql_outcomes = f"""
        SELECT {_DM_OUTCOME_CASE} AS outcome,
               COUNT(p.id) AS pieces,
               COALESCE(SUM(p.cost_cents), 0) AS cost_cents
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.id = %s AND s.organization_id = %s
        GROUP BY outcome
    """
    sql_status = """
        SELECT p.status, COUNT(p.id)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.id = %s AND s.organization_id = %s
        GROUP BY p.status
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql_outcomes,
                (start, end, str(step_id), str(organization_id)),
            )
            outcome_rows = await cur.fetchall()
            await cur.execute(
                sql_status,
                (start, end, str(step_id), str(organization_id)),
            )
            status_rows = await cur.fetchall()

    outcomes = _empty(_OUTCOME_KEYS)
    cost_total_cents = 0
    events_total = 0
    for outcome, pieces, cost_cents in outcome_rows:
        n = int(pieces)
        outcomes[outcome] = outcomes.get(outcome, 0) + n
        cost_total_cents += int(cost_cents or 0)
        events_total += n

    piece_funnel = _empty(_PIECE_FUNNEL_KEYS)
    for status_, count in status_rows:
        bucket = _PIECE_STATUS_TO_FUNNEL.get(status_)
        if bucket is not None:
            piece_funnel[bucket] += int(count)

    return {
        "events_total": events_total,
        "cost_total_cents": cost_total_cents,
        "outcomes": outcomes,
        "piece_funnel": piece_funnel,
    }


async def _load_dm_event_breakdown(
    *,
    organization_id: UUID,
    step_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Count direct_mail_piece_events by event_type for pieces tied to the step.

    Joined through direct_mail_pieces so org isolation flows from the step
    row (which is filtered by ``s.organization_id``) into the events log.
    """
    sql = """
        SELECT e.event_type, COUNT(*)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
        JOIN direct_mail_piece_events e
          ON e.piece_id = p.id
         AND e.received_at >= %s
         AND e.received_at < %s
        WHERE s.id = %s AND s.organization_id = %s
        GROUP BY e.event_type
        ORDER BY e.event_type
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql, (start, end, str(step_id), str(organization_id))
            )
            rows = await cur.fetchall()
    return {event_type: int(count) for event_type, count in rows}


async def summarize_step(
    *,
    organization_id: UUID,
    step_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Build the per-step summary payload.

    Raises :class:`StepNotFound` when the step does not belong to the
    caller's organization. The router maps this to 404 so callers in
    other orgs cannot probe step existence.
    """
    step = await _load_step(
        organization_id=organization_id, step_id=step_id
    )
    memberships = await _load_memberships(
        organization_id=organization_id, step_id=step_id
    )

    if step["channel"] == "direct_mail":
        dm = await _load_dm_aggregates(
            organization_id=organization_id,
            step_id=step_id,
            start=start,
            end=end,
        )
        by_event_type = await _load_dm_event_breakdown(
            organization_id=organization_id,
            step_id=step_id,
            start=start,
            end=end,
        )
        events_block = {
            "total": dm["events_total"],
            "by_event_type": by_event_type,
            "outcomes": dm["outcomes"],
            "cost_total_cents": dm["cost_total_cents"],
        }
        channel_specific: dict[str, Any] = {
            "direct_mail": {"piece_funnel": dm["piece_funnel"]},
        }
    else:
        # Real step rows for non-direct-mail channels exist (back-filled by
        # 0023 with one default step per channel_campaign), but no per-step
        # artifact rows are wired yet — voice/sms aggregates live at the
        # channel_campaign rollup layer with the synthetic-step fallback.
        events_block = {
            "total": 0,
            "by_event_type": {},
            "outcomes": _empty(_OUTCOME_KEYS),
            "cost_total_cents": 0,
        }
        channel_specific = {}

    return {
        "step": step,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "events": events_block,
        "memberships": memberships,
        "channel_specific": channel_specific,
        "source": "postgres",
    }


__all__ = ["StepNotFound", "summarize_step"]
