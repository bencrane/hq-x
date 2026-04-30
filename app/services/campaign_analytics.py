"""Campaign rollup analytics — per-campaign / per-channel_campaign / per-step.

Given an umbrella ``business.campaigns`` row, aggregate every channel
campaign under it, every step inside each channel campaign, and the
per-step / per-channel events, costs, outcomes, and unique-recipient
counts.

Org isolation: the campaign must belong to the caller's
``organization_id`` or the service raises ``CampaignNotFound`` (the
router maps that to 404 — never leak existence).

Postgres-only by design (per ``DIRECTIVE_HQX_ANALYTICS_REMAINDER.md``
§1.1): every payload returned carries ``"source": "postgres"``. No
ClickHouse branching.

Voice/SMS step + recipient wiring is deferred (§1.4): ``call_logs`` and
``sms_messages`` carry neither ``channel_campaign_step_id`` nor
``recipient_id`` today. For those channels we surface a single
*synthetic* step per channel campaign, and tag the channel campaign
with ``"voice_step_attribution": "synthetic"`` (or the SMS analog) so
consumers know the step granularity is degenerate.

Outcome mapping (status → succeeded / failed / skipped):

* direct_mail_pieces.status:
    * delivered, mailed, in_transit, processed_for_delivery,
      in_local_area  → succeeded
    * failed, returned, rejected                              → failed
    * everything else (unknown, created, processing)          → skipped
* call_logs.outcome:
    * qualified_transfer, interested  → succeeded
    * do_not_call, failed             → failed
    * everything else                 → skipped
  (``interested`` is included so that the future addition of that
   outcome bucket Just Works; current schema doesn't define it but the
   directive lists it explicitly as a successful outcome.)
* sms_messages.status:
    * delivered             → succeeded
    * failed, undelivered   → failed
    * everything else       → skipped

Every outcome bucket is computed in SQL with a CASE expression so the
Python layer only has to slot the row into the right output bucket.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection


class CampaignNotFound(Exception):
    """Raised when the campaign id isn't in the caller's organization."""


# ── outcome mapping (kept in SQL; mirrored here for readability) ────────

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

_CALL_OUTCOME_CASE = """
    CASE
        WHEN cl.outcome IN ('qualified_transfer', 'interested')
            THEN 'succeeded'
        WHEN cl.outcome IN ('do_not_call', 'failed')
            THEN 'failed'
        ELSE 'skipped'
    END
"""

_SMS_OUTCOME_CASE = """
    CASE
        WHEN sm.status = 'delivered' THEN 'succeeded'
        WHEN sm.status IN ('failed', 'undelivered') THEN 'failed'
        ELSE 'skipped'
    END
"""

_DELIVERED_OR_INTRANSIT_STATUSES = (
    "delivered",
    "mailed",
    "in_transit",
    "processed_for_delivery",
    "in_local_area",
)

_OUTCOME_KEYS = ("succeeded", "failed", "skipped")
_MEMBERSHIP_KEYS = (
    "pending",
    "scheduled",
    "sent",
    "failed",
    "suppressed",
    "cancelled",
)


def _empty_outcomes() -> dict[str, int]:
    return {k: 0 for k in _OUTCOME_KEYS}


def _empty_memberships() -> dict[str, int]:
    return {k: 0 for k in _MEMBERSHIP_KEYS}


async def _load_campaign(
    *, organization_id: UUID, campaign_id: UUID
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, organization_id, brand_id, name, status,
                       start_date, created_at
                FROM business.campaigns
                WHERE id = %s AND organization_id = %s
                """,
                (str(campaign_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise CampaignNotFound(
            f"campaign {campaign_id} not found in organization {organization_id}"
        )
    return {
        "id": str(row[0]),
        "organization_id": str(row[1]),
        "brand_id": str(row[2]),
        "name": row[3],
        "status": row[4],
        "start_date": row[5].isoformat() if row[5] is not None else None,
        "created_at": row[6].isoformat() if row[6] is not None else None,
    }


async def _load_channel_campaigns(
    *, organization_id: UUID, campaign_id: UUID
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, channel, provider, status, scheduled_send_at
                FROM business.channel_campaigns
                WHERE campaign_id = %s AND organization_id = %s
                ORDER BY created_at
                """,
                (str(campaign_id), str(organization_id)),
            )
            rows = await cur.fetchall()
    return [
        {
            "channel_campaign_id": str(r[0]),
            "name": r[1],
            "channel": r[2],
            "provider": r[3],
            "status": r[4],
            "scheduled_send_at": r[5].isoformat() if r[5] is not None else None,
        }
        for r in rows
    ]


async def _load_steps(
    *, organization_id: UUID, campaign_id: UUID
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, channel_campaign_id, step_order, name,
                       external_provider_id
                FROM business.channel_campaign_steps
                WHERE campaign_id = %s AND organization_id = %s
                ORDER BY channel_campaign_id, step_order
                """,
                (str(campaign_id), str(organization_id)),
            )
            rows = await cur.fetchall()
    return [
        {
            "channel_campaign_step_id": str(r[0]),
            "channel_campaign_id": str(r[1]),
            "step_order": r[2],
            "name": r[3],
            "external_provider_id": r[4],
            "events_total": 0,
            "cost_total_cents": 0,
            "outcomes": _empty_outcomes(),
            "memberships": _empty_memberships(),
        }
        for r in rows
    ]


async def _load_dm_step_aggregates(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, Any]]:
    """Per-step direct_mail aggregates keyed by channel_campaign_step_id."""
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
        WHERE s.campaign_id = %s
          AND s.organization_id = %s
        GROUP BY s.id, outcome
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql, (start, end, str(campaign_id), str(organization_id))
            )
            rows = await cur.fetchall()

    by_step: dict[str, dict[str, Any]] = {}
    for step_id, pieces, cost_cents, outcome in rows:
        sid = str(step_id)
        bucket = by_step.setdefault(
            sid,
            {
                "events_total": 0,
                "cost_total_cents": 0,
                "outcomes": _empty_outcomes(),
            },
        )
        # outcome rows where pieces=0 are emitted because of the LEFT JOIN
        # group; they correspond to no real piece — skip them.
        if pieces == 0:
            continue
        bucket["events_total"] += int(pieces)
        bucket["cost_total_cents"] += int(cost_cents or 0)
        bucket["outcomes"][outcome] = (
            bucket["outcomes"].get(outcome, 0) + int(pieces)
        )
    return by_step


async def _load_step_memberships(
    *,
    organization_id: UUID,
    campaign_id: UUID,
) -> dict[str, dict[str, int]]:
    """Per-step membership status counts."""
    sql = """
        SELECT scr.channel_campaign_step_id, scr.status, COUNT(*)
        FROM business.channel_campaign_step_recipients scr
        JOIN business.channel_campaign_steps s
          ON s.id = scr.channel_campaign_step_id
        WHERE s.campaign_id = %s
          AND s.organization_id = %s
          AND scr.organization_id = %s
        GROUP BY scr.channel_campaign_step_id, scr.status
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    str(campaign_id),
                    str(organization_id),
                    str(organization_id),
                ),
            )
            rows = await cur.fetchall()

    by_step: dict[str, dict[str, int]] = {}
    for step_id, status_, count in rows:
        sid = str(step_id)
        bucket = by_step.setdefault(sid, _empty_memberships())
        bucket[status_] = int(count)
    return by_step


async def _load_dm_unique_recipients_per_cc(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Unique recipient count per channel_campaign for direct_mail."""
    sql = """
        SELECT s.channel_campaign_id, COUNT(DISTINCT p.recipient_id)
        FROM business.channel_campaign_steps s
        JOIN direct_mail_pieces p
          ON p.channel_campaign_step_id = s.id
         AND p.deleted_at IS NULL
         AND p.recipient_id IS NOT NULL
         AND p.created_at >= %s
         AND p.created_at < %s
        WHERE s.campaign_id = %s
          AND s.organization_id = %s
        GROUP BY s.channel_campaign_id
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql, (start, end, str(campaign_id), str(organization_id))
            )
            rows = await cur.fetchall()
    return {str(cc): int(count) for cc, count in rows}


async def _load_dub_conversions(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Click funnel rolled up across every direct_mail step in the campaign.

    Two queries — clicks aggregate (via dmaas_dub_events → dmaas_dub_links →
    channel_campaign_steps) and the unique-recipients denominator
    (delivered/in-transit-family pieces in the window). Both filter by
    ``s.campaign_id`` + ``s.organization_id``.
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
          AND s.campaign_id = %s
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
        WHERE s.campaign_id = %s AND s.organization_id = %s
    """
    sql_leads = """
        SELECT COUNT(*) AS leads_total,
               COUNT(DISTINCT ls.recipient_id) AS unique_leads
        FROM business.landing_page_submissions ls
        WHERE ls.campaign_id = %s
          AND ls.organization_id = %s
          AND ls.submitted_at >= %s AND ls.submitted_at < %s
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql_clicks,
                (start, end, str(campaign_id), str(organization_id)),
            )
            click_row = await cur.fetchone()
            await cur.execute(
                sql_denom,
                (
                    start,
                    end,
                    *_DELIVERED_OR_INTRANSIT_STATUSES,
                    str(campaign_id),
                    str(organization_id),
                ),
            )
            denom_row = await cur.fetchone()
            await cur.execute(
                sql_leads,
                (str(campaign_id), str(organization_id), start, end),
            )
            lead_row = await cur.fetchone()
    clicks_total = int(click_row[0]) if click_row and click_row[0] else 0
    unique_clickers = int(click_row[1]) if click_row and click_row[1] else 0
    denom = int(denom_row[0]) if denom_row and denom_row[0] else 0
    click_rate = (unique_clickers / denom) if denom else 0.0
    leads_total = int(lead_row[0]) if lead_row and lead_row[0] else 0
    unique_leads = int(lead_row[1]) if lead_row and lead_row[1] else 0
    lead_rate = (unique_leads / unique_clickers) if unique_clickers else 0.0
    return {
        "clicks_total": clicks_total,
        "unique_clickers": unique_clickers,
        "click_rate": round(click_rate, 4),
        "leads_total": leads_total,
        "unique_leads": unique_leads,
        "lead_rate": round(lead_rate, 4),
    }


async def _load_voice_aggregates(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, Any]]:
    """Voice rollup per channel_campaign (synthetic step granularity).

    ``call_logs`` carries ``channel_campaign_id`` (renamed from ``campaign_id``
    in 0022) and ``brand_id``; the join goes through
    ``business.channel_campaigns`` to bind it to ``campaign_id`` + verify
    org isolation.
    """
    sql = f"""
        SELECT cc.id AS channel_campaign_id,
               COUNT(cl.id) AS calls,
               COALESCE(SUM(COALESCE(cl.cost_total::numeric, 0)), 0)
                 AS cost_total_dollars,
               {_CALL_OUTCOME_CASE} AS outcome
        FROM business.channel_campaigns cc
        LEFT JOIN call_logs cl
          ON cl.channel_campaign_id = cc.id
         AND cl.deleted_at IS NULL
         AND cl.created_at >= %s
         AND cl.created_at < %s
        WHERE cc.campaign_id = %s
          AND cc.organization_id = %s
          AND cc.channel = 'voice_outbound'
        GROUP BY cc.id, outcome
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql, (start, end, str(campaign_id), str(organization_id))
            )
            rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for cc_id, calls, cost_dollars, outcome in rows:
        ccid = str(cc_id)
        bucket = out.setdefault(
            ccid,
            {
                "events_total": 0,
                "cost_total_cents": 0,
                "outcomes": _empty_outcomes(),
            },
        )
        if calls == 0:
            continue
        bucket["events_total"] += int(calls)
        # call_logs.cost_total is NUMERIC dollars; convert to cents.
        bucket["cost_total_cents"] += int(round(float(cost_dollars or 0) * 100))
        bucket["outcomes"][outcome] = (
            bucket["outcomes"].get(outcome, 0) + int(calls)
        )
    return out


async def _load_sms_aggregates(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, Any]]:
    """SMS rollup per channel_campaign (synthetic step granularity)."""
    sql = f"""
        SELECT cc.id AS channel_campaign_id,
               COUNT(sm.id) AS msgs,
               {_SMS_OUTCOME_CASE} AS outcome
        FROM business.channel_campaigns cc
        LEFT JOIN sms_messages sm
          ON sm.channel_campaign_id = cc.id
         AND sm.created_at >= %s
         AND sm.created_at < %s
        WHERE cc.campaign_id = %s
          AND cc.organization_id = %s
          AND cc.channel = 'sms'
        GROUP BY cc.id, outcome
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql, (start, end, str(campaign_id), str(organization_id))
            )
            rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for cc_id, msgs, outcome in rows:
        ccid = str(cc_id)
        bucket = out.setdefault(
            ccid,
            {
                "events_total": 0,
                "cost_total_cents": 0,
                "outcomes": _empty_outcomes(),
            },
        )
        if msgs == 0:
            continue
        bucket["events_total"] += int(msgs)
        bucket["outcomes"][outcome] = (
            bucket["outcomes"].get(outcome, 0) + int(msgs)
        )
    return out


def _synthetic_step(channel: str) -> dict[str, Any]:
    return {
        "channel_campaign_step_id": None,
        "step_order": 0,
        "name": "(synthetic)",
        "external_provider_id": None,
        "events_total": 0,
        "cost_total_cents": 0,
        "outcomes": _empty_outcomes(),
        "memberships": _empty_memberships(),
        "synthetic": True,
        "channel": channel,
    }


async def summarize_campaign(
    *,
    organization_id: UUID,
    campaign_id: UUID,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Roll up everything under a campaign for the given window.

    Raises :class:`CampaignNotFound` if the campaign does not belong to
    the supplied organization. The caller (router) maps this to 404; we
    never leak existence across orgs.
    """
    campaign = await _load_campaign(
        organization_id=organization_id, campaign_id=campaign_id
    )
    channel_campaigns = await _load_channel_campaigns(
        organization_id=organization_id, campaign_id=campaign_id
    )
    steps = await _load_steps(
        organization_id=organization_id, campaign_id=campaign_id
    )
    dm_steps = await _load_dm_step_aggregates(
        organization_id=organization_id,
        campaign_id=campaign_id,
        start=start,
        end=end,
    )
    memberships = await _load_step_memberships(
        organization_id=organization_id, campaign_id=campaign_id
    )
    dm_unique_recipients = await _load_dm_unique_recipients_per_cc(
        organization_id=organization_id,
        campaign_id=campaign_id,
        start=start,
        end=end,
    )
    voice_aggs = await _load_voice_aggregates(
        organization_id=organization_id,
        campaign_id=campaign_id,
        start=start,
        end=end,
    )
    sms_aggs = await _load_sms_aggregates(
        organization_id=organization_id,
        campaign_id=campaign_id,
        start=start,
        end=end,
    )
    conversions = await _load_dub_conversions(
        organization_id=organization_id,
        campaign_id=campaign_id,
        start=start,
        end=end,
    )

    # Decorate steps with their per-step direct_mail aggregates +
    # memberships, then bucket by channel_campaign.
    steps_by_cc: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        sid = step["channel_campaign_step_id"]
        if sid in dm_steps:
            agg = dm_steps[sid]
            step["events_total"] = agg["events_total"]
            step["cost_total_cents"] = agg["cost_total_cents"]
            step["outcomes"] = agg["outcomes"]
        if sid in memberships:
            step["memberships"] = memberships[sid]
        steps_by_cc.setdefault(step["channel_campaign_id"], []).append(step)

    cc_objects: list[dict[str, Any]] = []
    by_channel: dict[str, dict[str, Any]] = {}
    by_provider: dict[str, dict[str, Any]] = {}
    totals_events = 0
    totals_cost_cents = 0
    totals_unique_recipients = 0

    for cc in channel_campaigns:
        ccid = cc["channel_campaign_id"]
        channel = cc["channel"]
        provider = cc["provider"]
        cc_steps = steps_by_cc.get(ccid, [])
        cc_events = 0
        cc_cost = 0
        cc_outcomes = _empty_outcomes()
        cc_unique_recipients = 0

        if channel == "direct_mail":
            for step in cc_steps:
                cc_events += step["events_total"]
                cc_cost += step["cost_total_cents"]
                for k, v in step["outcomes"].items():
                    cc_outcomes[k] = cc_outcomes.get(k, 0) + v
            cc_unique_recipients = dm_unique_recipients.get(ccid, 0)
            cc_obj = {
                **cc,
                "events_total": cc_events,
                "unique_recipients": cc_unique_recipients,
                "cost_total_cents": cc_cost,
                "outcomes": cc_outcomes,
                "steps": cc_steps,
            }
        elif channel == "voice_outbound":
            agg = voice_aggs.get(ccid)
            if agg is not None:
                cc_events = agg["events_total"]
                cc_cost = agg["cost_total_cents"]
                cc_outcomes = agg["outcomes"]
            synthetic = _synthetic_step(channel)
            synthetic["events_total"] = cc_events
            synthetic["cost_total_cents"] = cc_cost
            synthetic["outcomes"] = dict(cc_outcomes)
            cc_obj = {
                **cc,
                "events_total": cc_events,
                "unique_recipients": 0,
                "cost_total_cents": cc_cost,
                "outcomes": cc_outcomes,
                "steps": [synthetic],
                "voice_step_attribution": "synthetic",
            }
        elif channel == "sms":
            agg = sms_aggs.get(ccid)
            if agg is not None:
                cc_events = agg["events_total"]
                cc_outcomes = agg["outcomes"]
            synthetic = _synthetic_step(channel)
            synthetic["events_total"] = cc_events
            synthetic["outcomes"] = dict(cc_outcomes)
            cc_obj = {
                **cc,
                "events_total": cc_events,
                "unique_recipients": 0,
                "cost_total_cents": 0,
                "outcomes": cc_outcomes,
                "steps": [synthetic],
                "sms_step_attribution": "synthetic",
            }
        else:  # email or anything else not yet wired
            for step in cc_steps:
                cc_events += step["events_total"]
                cc_cost += step["cost_total_cents"]
                for k, v in step["outcomes"].items():
                    cc_outcomes[k] = cc_outcomes.get(k, 0) + v
            cc_obj = {
                **cc,
                "events_total": cc_events,
                "unique_recipients": 0,
                "cost_total_cents": cc_cost,
                "outcomes": cc_outcomes,
                "steps": cc_steps,
            }

        cc_objects.append(cc_obj)
        totals_events += cc_events
        totals_cost_cents += cc_cost
        totals_unique_recipients += cc_unique_recipients

        ch_bucket = by_channel.setdefault(
            channel,
            {
                "channel": channel,
                "events_total": 0,
                "unique_recipients": 0,
                "outcomes": _empty_outcomes(),
                "cost_total_cents": 0,
            },
        )
        ch_bucket["events_total"] += cc_events
        ch_bucket["unique_recipients"] += cc_unique_recipients
        ch_bucket["cost_total_cents"] += cc_cost
        for k, v in cc_outcomes.items():
            ch_bucket["outcomes"][k] = ch_bucket["outcomes"].get(k, 0) + v

        prov_bucket = by_provider.setdefault(
            provider,
            {
                "provider": provider,
                "events_total": 0,
                "outcomes": _empty_outcomes(),
                "cost_total_cents": 0,
            },
        )
        prov_bucket["events_total"] += cc_events
        prov_bucket["cost_total_cents"] += cc_cost
        for k, v in cc_outcomes.items():
            prov_bucket["outcomes"][k] = prov_bucket["outcomes"].get(k, 0) + v

    return {
        "campaign": campaign,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "totals": {
            "events_total": totals_events,
            "unique_recipients_total": totals_unique_recipients,
            "cost_total_cents": totals_cost_cents,
        },
        "conversions": conversions,
        "channel_campaigns": cc_objects,
        "by_channel": list(by_channel.values()),
        "by_provider": list(by_provider.values()),
        "source": "postgres",
    }


__all__ = ["CampaignNotFound", "summarize_campaign"]
