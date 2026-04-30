"""Per-channel_campaign analytics — drilldown for one channel_campaign.

Same shape as the campaign rollup (``app/services/campaign_analytics.py``)
but scoped to a single channel_campaign id, with a channel-specific
extensions block:

* direct_mail → ``piece_funnel`` (queued / processed / in_transit /
  delivered / returned / failed).
* voice_outbound → ``transfer_rate``, ``avg_duration_seconds``,
  ``cost_breakdown`` (transport / stt / llm / tts / vapi),
  ``voice_step_attribution: "synthetic"``.
* sms → ``delivery_rate``, ``opt_out_count``,
  ``sms_step_attribution: "synthetic"``.
* email → zeros (EmailBison adapter shipped in #32 but no per-recipient
  artifact rows tagged with channel_campaign_step_id yet, so the
  channel_specific block stays minimal).

Org isolation: the channel_campaign SELECT combines id +
organization_id in a single WHERE clause — cross-org → 404.

Postgres-only; ``"source": "postgres"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services.campaign_analytics import (
    _CALL_OUTCOME_CASE,
    _DM_OUTCOME_CASE,
    _SMS_OUTCOME_CASE,
    _empty_memberships,
    _empty_outcomes,
    _synthetic_step,
)


class ChannelCampaignNotFound(Exception):
    """Raised when the channel_campaign isn't in the caller's org."""


_PIECE_FUNNEL_KEYS = (
    "queued",
    "processed",
    "in_transit",
    "delivered",
    "returned",
    "failed",
)

_DELIVERED_OR_INTRANSIT_STATUSES = (
    "delivered",
    "mailed",
    "in_transit",
    "processed_for_delivery",
    "in_local_area",
)
_PIECE_STATUS_TO_FUNNEL = {
    "queued": "queued",
    "processing": "queued",
    "created": "queued",
    "unknown": "queued",
    "processed": "processed",
    "ready_for_mail": "processed",
    "mailed": "processed",
    "in_transit": "in_transit",
    "in_local_area": "in_transit",
    "processed_for_delivery": "in_transit",
    "delivered": "delivered",
    "returned": "returned",
    "failed": "failed",
    "rejected": "failed",
}


async def _load_channel_campaign(
    *, organization_id: UUID, channel_campaign_id: UUID
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, campaign_id, name, channel, provider, status,
                       scheduled_send_at, brand_id, organization_id
                FROM business.channel_campaigns
                WHERE id = %s AND organization_id = %s
                """,
                (str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise ChannelCampaignNotFound(
            f"channel_campaign {channel_campaign_id} not found in org "
            f"{organization_id}"
        )
    return {
        "id": str(row[0]),
        "campaign_id": str(row[1]),
        "name": row[2],
        "channel": row[3],
        "provider": row[4],
        "status": row[5],
        "scheduled_send_at": row[6].isoformat() if row[6] is not None else None,
        "brand_id": str(row[7]),
        "organization_id": str(row[8]),
    }


async def _load_steps(
    *, organization_id: UUID, channel_campaign_id: UUID
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, step_order, name, external_provider_id
                FROM business.channel_campaign_steps
                WHERE channel_campaign_id = %s AND organization_id = %s
                ORDER BY step_order
                """,
                (str(channel_campaign_id), str(organization_id)),
            )
            rows = await cur.fetchall()
    return [
        {
            "channel_campaign_step_id": str(r[0]),
            "channel_campaign_id": str(channel_campaign_id),
            "step_order": r[1],
            "name": r[2],
            "external_provider_id": r[3],
            "events_total": 0,
            "cost_total_cents": 0,
            "outcomes": _empty_outcomes(),
            "memberships": _empty_memberships(),
        }
        for r in rows
    ]


async def _load_dm_step_aggs(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Per-step direct_mail aggregates + the per-cc piece funnel."""
    sql = f"""
        SELECT s.id AS step_id,
               COUNT(p.id) AS pieces,
               COALESCE(SUM(p.cost_cents), 0) AS cost_cents,
               {_DM_OUTCOME_CASE} AS outcome
        FROM business.channel_campaign_steps s
        LEFT JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.channel_campaign_id = %s
          AND s.organization_id = %s
        GROUP BY s.id, outcome
    """
    sql_funnel = """
        SELECT p.status, COUNT(*)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.channel_campaign_id = %s
          AND s.organization_id = %s
        GROUP BY p.status
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (start, end, str(channel_campaign_id), str(organization_id)),
            )
            agg_rows = await cur.fetchall()
            await cur.execute(
                sql_funnel,
                (start, end, str(channel_campaign_id), str(organization_id)),
            )
            status_rows = await cur.fetchall()

    by_step: dict[str, dict[str, Any]] = {}
    for step_id, pieces, cost_cents, outcome in agg_rows:
        sid = str(step_id)
        bucket = by_step.setdefault(
            sid,
            {
                "events_total": 0,
                "cost_total_cents": 0,
                "outcomes": _empty_outcomes(),
            },
        )
        if pieces == 0:
            continue
        bucket["events_total"] += int(pieces)
        bucket["cost_total_cents"] += int(cost_cents or 0)
        bucket["outcomes"][outcome] = (
            bucket["outcomes"].get(outcome, 0) + int(pieces)
        )

    funnel = {k: 0 for k in _PIECE_FUNNEL_KEYS}
    for status_, count in status_rows:
        bucket_name = _PIECE_STATUS_TO_FUNNEL.get(status_)
        if bucket_name is not None:
            funnel[bucket_name] += int(count)
    return by_step, funnel


async def _load_step_memberships(
    *, organization_id: UUID, channel_campaign_id: UUID
) -> dict[str, dict[str, int]]:
    sql = """
        SELECT scr.channel_campaign_step_id, scr.status, COUNT(*)
        FROM business.channel_campaign_step_recipients scr
        JOIN business.channel_campaign_steps s
          ON s.id = scr.channel_campaign_step_id
        WHERE s.channel_campaign_id = %s
          AND s.organization_id = %s
          AND scr.organization_id = %s
        GROUP BY scr.channel_campaign_step_id, scr.status
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    str(channel_campaign_id),
                    str(organization_id),
                    str(organization_id),
                ),
            )
            rows = await cur.fetchall()
    out: dict[str, dict[str, int]] = {}
    for step_id, status_, count in rows:
        sid = str(step_id)
        bucket = out.setdefault(sid, _empty_memberships())
        bucket[status_] = int(count)
    return out


async def _load_dm_unique_recipients(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> int:
    sql = """
        SELECT COUNT(DISTINCT p.recipient_id)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.recipient_id IS NOT NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.channel_campaign_id = %s
          AND s.organization_id = %s
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (start, end, str(channel_campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _load_dub_conversions(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Click funnel rolled up across every step in the channel_campaign.

    Two queries — clicks (event-side) and the unique-recipients
    denominator (piece-side, restricted to delivered/in-transit family).
    """
    sql_clicks = """
        SELECT COUNT(*) AS clicks_total,
               COUNT(DISTINCT dl.recipient_id) AS unique_clickers
        FROM dmaas_dub_events de
        JOIN dmaas_dub_links dl ON dl.dub_link_id = de.dub_link_id
        JOIN business.channel_campaign_steps s
          ON s.id = dl.channel_campaign_step_id
        WHERE de.event_type = 'link.clicked'
          AND de.occurred_at >= %s AND de.occurred_at < %s
          AND s.channel_campaign_id = %s
          AND s.organization_id = %s
    """
    placeholders = ", ".join(["%s"] * len(_DELIVERED_OR_INTRANSIT_STATUSES))
    sql_denom = f"""
        SELECT COUNT(DISTINCT p.recipient_id)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.recipient_id IS NOT NULL
         AND p.created_at >= %s AND p.created_at < %s
         AND p.status IN ({placeholders})
        WHERE s.channel_campaign_id = %s AND s.organization_id = %s
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql_clicks,
                (start, end, str(channel_campaign_id), str(organization_id)),
            )
            click_row = await cur.fetchone()
            await cur.execute(
                sql_denom,
                (
                    start,
                    end,
                    *_DELIVERED_OR_INTRANSIT_STATUSES,
                    str(channel_campaign_id),
                    str(organization_id),
                ),
            )
            denom_row = await cur.fetchone()
    clicks_total = int(click_row[0]) if click_row and click_row[0] else 0
    unique_clickers = int(click_row[1]) if click_row and click_row[1] else 0
    denom = int(denom_row[0]) if denom_row and denom_row[0] else 0
    click_rate = (unique_clickers / denom) if denom else 0.0
    return {
        "clicks_total": clicks_total,
        "unique_clickers": unique_clickers,
        "click_rate": round(click_rate, 4),
    }


async def _load_voice_extension(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    brand_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Voice rollup + channel_specific.voice_outbound block.

    call_logs doesn't carry organization_id; the org isolation guard is
    `cl.brand_id = %s` where the brand has already been verified to be
    in the auth's org via the channel_campaign lookup. (The
    channel_campaign row carries brand_id directly and was filtered by
    organization_id, so this is safe.)
    """
    sql_outcomes = f"""
        SELECT {_CALL_OUTCOME_CASE} AS outcome,
               COUNT(*) AS calls,
               COALESCE(SUM(COALESCE(cl.cost_total::numeric, 0)), 0)
                 AS cost_total_dollars,
               COALESCE(SUM(COALESCE(cl.duration_seconds, 0)), 0)
                 AS dur_seconds
        FROM call_logs cl
        WHERE cl.channel_campaign_id = %s
          AND cl.brand_id = %s
          AND cl.deleted_at IS NULL
          AND cl.created_at >= %s
          AND cl.created_at < %s
        GROUP BY outcome
    """
    sql_costs = """
        SELECT
            COALESCE(SUM((cl.cost_breakdown->>'transport')::numeric), 0),
            COALESCE(SUM((cl.cost_breakdown->>'stt')::numeric), 0),
            COALESCE(SUM((cl.cost_breakdown->>'llm')::numeric), 0),
            COALESCE(SUM((cl.cost_breakdown->>'tts')::numeric), 0),
            COALESCE(SUM((cl.cost_breakdown->>'vapi')::numeric), 0)
        FROM call_logs cl
        WHERE cl.channel_campaign_id = %s
          AND cl.brand_id = %s
          AND cl.deleted_at IS NULL
          AND cl.created_at >= %s
          AND cl.created_at < %s
    """
    args = (str(channel_campaign_id), str(brand_id), start, end)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql_outcomes, args)
            outcome_rows = await cur.fetchall()
            await cur.execute(sql_costs, args)
            cost_row = await cur.fetchone()

    outcomes = _empty_outcomes()
    total_calls = 0
    transferred = 0
    total_dur = 0
    total_cost_cents = 0
    for outcome, calls, cost_dollars, dur_seconds in outcome_rows:
        n = int(calls)
        outcomes[outcome] = outcomes.get(outcome, 0) + n
        total_calls += n
        total_dur += int(dur_seconds or 0)
        total_cost_cents += int(round(float(cost_dollars or 0) * 100))
        if outcome == "succeeded":
            transferred += n

    transfer_rate = (transferred / total_calls) if total_calls else 0.0
    avg_dur = (total_dur // total_calls) if total_calls else 0
    cost_breakdown = {
        "transport": float(cost_row[0]) if cost_row else 0.0,
        "stt": float(cost_row[1]) if cost_row else 0.0,
        "llm": float(cost_row[2]) if cost_row else 0.0,
        "tts": float(cost_row[3]) if cost_row else 0.0,
        "vapi": float(cost_row[4]) if cost_row else 0.0,
    }
    return {
        "events_total": total_calls,
        "cost_total_cents": total_cost_cents,
        "outcomes": outcomes,
        "channel_specific": {
            "voice_outbound": {
                "transfer_rate": round(transfer_rate, 4),
                "avg_duration_seconds": avg_dur,
                "cost_breakdown": cost_breakdown,
                "voice_step_attribution": "synthetic",
            }
        },
    }


async def _load_sms_extension(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    brand_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """SMS rollup + channel_specific.sms block."""
    sql = f"""
        SELECT {_SMS_OUTCOME_CASE} AS outcome,
               COUNT(*) AS msgs
        FROM sms_messages sm
        WHERE sm.channel_campaign_id = %s
          AND sm.brand_id = %s
          AND sm.created_at >= %s
          AND sm.created_at < %s
        GROUP BY outcome
    """
    sql_optout = """
        SELECT COUNT(*) FROM sms_messages sm
        WHERE sm.channel_campaign_id = %s
          AND sm.brand_id = %s
          AND sm.created_at >= %s
          AND sm.created_at < %s
          AND (
              UPPER(COALESCE(sm.body, '')) ~ '\\b(STOP|UNSUBSCRIBE|CANCEL|END|QUIT)\\b'
              OR sm.status IN ('canceled')
          )
    """
    args = (str(channel_campaign_id), str(brand_id), start, end)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, args)
            outcome_rows = await cur.fetchall()
            await cur.execute(sql_optout, args)
            optout_row = await cur.fetchone()

    outcomes = _empty_outcomes()
    total_msgs = 0
    delivered = 0
    for outcome, msgs in outcome_rows:
        n = int(msgs)
        outcomes[outcome] = outcomes.get(outcome, 0) + n
        total_msgs += n
        if outcome == "succeeded":
            delivered += n
    delivery_rate = (delivered / total_msgs) if total_msgs else 0.0
    opt_out_count = int(optout_row[0]) if optout_row and optout_row[0] else 0

    return {
        "events_total": total_msgs,
        "cost_total_cents": 0,
        "outcomes": outcomes,
        "channel_specific": {
            "sms": {
                "delivery_rate": round(delivery_rate, 4),
                "opt_out_count": opt_out_count,
                "sms_step_attribution": "synthetic",
            }
        },
    }


async def summarize_channel_campaign(
    *,
    organization_id: UUID,
    channel_campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Build the per-channel_campaign summary payload.

    Raises :class:`ChannelCampaignNotFound` when the channel_campaign
    does not belong to the caller's organization. The router maps to
    404 — never leak existence.
    """
    cc = await _load_channel_campaign(
        organization_id=organization_id,
        channel_campaign_id=channel_campaign_id,
    )
    channel = cc["channel"]
    brand_uuid = UUID(cc["brand_id"])

    if channel == "direct_mail":
        steps = await _load_steps(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
        )
        dm_step_aggs, piece_funnel = await _load_dm_step_aggs(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
            start=start,
            end=end,
        )
        memberships = await _load_step_memberships(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
        )
        unique_recipients = await _load_dm_unique_recipients(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
            start=start,
            end=end,
        )
        conversions = await _load_dub_conversions(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
            start=start,
            end=end,
        )
        events_total = 0
        cost_cents = 0
        outcomes = _empty_outcomes()
        for step in steps:
            sid = step["channel_campaign_step_id"]
            if sid in dm_step_aggs:
                agg = dm_step_aggs[sid]
                step["events_total"] = agg["events_total"]
                step["cost_total_cents"] = agg["cost_total_cents"]
                step["outcomes"] = agg["outcomes"]
            if sid in memberships:
                step["memberships"] = memberships[sid]
            events_total += step["events_total"]
            cost_cents += step["cost_total_cents"]
            for k, v in step["outcomes"].items():
                outcomes[k] = outcomes.get(k, 0) + v
        return {
            "channel_campaign": cc,
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "totals": {
                "events_total": events_total,
                "unique_recipients": unique_recipients,
                "cost_total_cents": cost_cents,
            },
            "conversions": conversions,
            "outcomes": outcomes,
            "steps": steps,
            "channel_specific": {
                "direct_mail": {"piece_funnel": piece_funnel}
            },
            "source": "postgres",
        }

    zero_conversions = {
        "clicks_total": 0,
        "unique_clickers": 0,
        "click_rate": 0.0,
    }

    if channel == "voice_outbound":
        ext = await _load_voice_extension(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
            brand_id=brand_uuid,
            start=start,
            end=end,
        )
        synthetic = _synthetic_step(channel)
        synthetic["events_total"] = ext["events_total"]
        synthetic["cost_total_cents"] = ext["cost_total_cents"]
        synthetic["outcomes"] = dict(ext["outcomes"])
        return {
            "channel_campaign": cc,
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "totals": {
                "events_total": ext["events_total"],
                "unique_recipients": 0,
                "cost_total_cents": ext["cost_total_cents"],
            },
            "conversions": zero_conversions,
            "outcomes": ext["outcomes"],
            "steps": [synthetic],
            "channel_specific": ext["channel_specific"],
            "source": "postgres",
        }

    if channel == "sms":
        ext = await _load_sms_extension(
            organization_id=organization_id,
            channel_campaign_id=channel_campaign_id,
            brand_id=brand_uuid,
            start=start,
            end=end,
        )
        synthetic = _synthetic_step(channel)
        synthetic["events_total"] = ext["events_total"]
        synthetic["outcomes"] = dict(ext["outcomes"])
        return {
            "channel_campaign": cc,
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "totals": {
                "events_total": ext["events_total"],
                "unique_recipients": 0,
                "cost_total_cents": 0,
            },
            "conversions": zero_conversions,
            "outcomes": ext["outcomes"],
            "steps": [synthetic],
            "channel_specific": ext["channel_specific"],
            "source": "postgres",
        }

    # email or any other channel — zero block, no per-channel extension
    # since EmailBison doesn't yet emit per-recipient analytics tagged
    # with channel_campaign_step_id. Steps still surface (they exist in
    # the schema even if no artifact rows do).
    steps = await _load_steps(
        organization_id=organization_id,
        channel_campaign_id=channel_campaign_id,
    )
    memberships = await _load_step_memberships(
        organization_id=organization_id,
        channel_campaign_id=channel_campaign_id,
    )
    for step in steps:
        sid = step["channel_campaign_step_id"]
        if sid in memberships:
            step["memberships"] = memberships[sid]
    return {
        "channel_campaign": cc,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "totals": {
            "events_total": 0,
            "unique_recipients": 0,
            "cost_total_cents": 0,
        },
        "conversions": zero_conversions,
        "outcomes": _empty_outcomes(),
        "steps": steps,
        "channel_specific": {channel: {}},
        "source": "postgres",
    }


__all__ = [
    "ChannelCampaignNotFound",
    "summarize_channel_campaign",
]
