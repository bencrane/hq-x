"""Cluster 3 health metrics — single SQL-backed snapshot for the dashboard.

Returns a structured dict with everything the operator needs to see at a
glance:

  * heartbeat — last pass / fail / age
  * dispatch — last hour: webhook_orphaned %, dispatch sent / failed /
    deferred / pending_review counts
  * stuck_queue — lead_transfers in 'queued' for > 5 min (red if any)
  * unclassified_backlog — classifications with status='unclassified'
    older than 1 hour (Anthropic outage signal)
  * pending_review — intros held by the verdict gate awaiting operator
    decision
  * reconciliation — last sweep: replies scanned / backfilled
  * partner_ledgers — for each active partner_contract, paid /
    delivered / remaining / window_days_remaining
  * source_material_freshness — partners w/o research artifact + recipients
    w/o gestalt
  * recent_alerts — last 20 cluster3_alerts rows
  * recent_dispatches — last 20 lead_transfers rows

Each section carries a status: 'green' | 'yellow' | 'red' so the dashboard
can render at a glance without re-deriving the rules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_db_connection

logger = logging.getLogger(__name__)


async def snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    return {
        "generated_at": now.isoformat(),
        # Cluster 3 (lead transfer / intro) section — pre-existing.
        "cluster_3": {
            "heartbeat": await _heartbeat_status(now=now),
            "dispatch_last_hour": await _dispatch_last_hour(now=now),
            "stuck_queue": await _stuck_queue(now=now),
            "unclassified_backlog": await _unclassified_backlog(now=now),
            "pending_review": await _pending_review(),
            "reconciliation": await _reconciliation_status(now=now),
            "partner_ledgers": await _partner_ledgers(now=now),
            "source_material_freshness": await _source_material_freshness(now=now),
        },
        # Cluster 1 (self-prospecting demand-side outreach) section.
        "cluster_1": {
            "outbound_heartbeat": await _outbound_heartbeat_status(
                now=now, cluster="cluster_1"
            ),
            "auto_reply_last_hour": await _cluster1_auto_reply_last_hour(now=now),
            "auto_reply_pending_review": await _cluster1_auto_reply_pending_review(),
            "deferred_disabled_count": await _cluster1_deferred_disabled_count(),
            "stuck_outbound": await _cluster_stuck_outbound(cluster="cluster_1"),
            "recent_auto_replies": await _cluster1_recent_auto_replies(),
        },
        # Cluster 2 (post-payment supply-side outreach) section.
        "cluster_2": {
            "outbound_heartbeat": await _outbound_heartbeat_status(
                now=now, cluster="cluster_2"
            ),
            "stuck_outbound": await _cluster_stuck_outbound(cluster="cluster_2"),
            "active_initiatives": await _cluster2_active_initiatives(now=now),
            "send_rate_last_hour": await _cluster2_send_rate_last_hour(now=now),
        },
        # Cross-cluster signals.
        "recent_alerts": await _recent_alerts(),
        "recent_dispatches": await _recent_dispatches(),
        "overall_status": "computed_below",
    }


async def overall_snapshot() -> dict[str, Any]:
    snap = await snapshot()
    cluster_statuses: list[str] = []
    for c_key in ("cluster_1", "cluster_2", "cluster_3"):
        for k, v in snap[c_key].items():
            if isinstance(v, dict) and "status" in v:
                cluster_statuses.append(v["status"])
    if "red" in cluster_statuses:
        overall = "red"
    elif "yellow" in cluster_statuses:
        overall = "yellow"
    else:
        overall = "green"
    snap["overall_status"] = overall
    return snap


# ── New per-cluster widgets ──────────────────────────────────────────────


async def _outbound_heartbeat_status(
    *, now: datetime, cluster: str
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, started_at, completed_at, duration_ms,
                       failure_reason
                FROM business.cluster_outbound_heartbeat_log
                WHERE cluster = %s
                ORDER BY started_at DESC LIMIT 1
                """,
                (cluster,),
            )
            row = await cur.fetchone()
            await cur.execute(
                """
                SELECT MAX(started_at)
                FROM business.cluster_outbound_heartbeat_log
                WHERE cluster = %s AND status = 'pass'
                """,
                (cluster,),
            )
            last_pass_row = await cur.fetchone()

    if row is None:
        return {
            "status": "yellow",
            "message": "No outbound heartbeat runs yet",
            "last_pass": None,
            "last_run": None,
        }

    last_pass = last_pass_row[0] if last_pass_row else None
    age = (now - last_pass).total_seconds() if last_pass else None
    status_color = "green" if (age is not None and age < 3600 * 2) else (
        "yellow" if (age is not None and age < 3600 * 6) else "red"
    )
    return {
        "status": status_color,
        "last_run_status": row[0],
        "last_run_started_at": row[1].isoformat() if row[1] else None,
        "last_run_duration_ms": row[3],
        "last_run_failure_reason": row[4],
        "last_pass_at": last_pass.isoformat() if last_pass else None,
        "last_pass_age_seconds": age,
    }


async def _cluster1_auto_reply_last_hour(*, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'pending_review') AS pending,
                    COUNT(*) FILTER (WHERE status = 'deferred_disabled') AS deferred,
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) AS total
                FROM business.cluster1_auto_replies
                WHERE created_at >= %s
                """,
                (cutoff,),
            )
            row = await cur.fetchone()
    sent, failed, pending, deferred, queued, total = row or (0,) * 6
    if (failed or 0) > 0:
        status = "red"
    elif (pending or 0) > 0 or (queued or 0) > 0:
        status = "yellow"
    else:
        status = "green"
    return {
        "status": status,
        "sent": sent or 0,
        "failed": failed or 0,
        "pending_review": pending or 0,
        "deferred_disabled": deferred or 0,
        "queued": queued or 0,
        "total": total or 0,
    }


async def _cluster1_auto_reply_pending_review() -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*), MIN(queued_at)
                FROM business.cluster1_auto_replies
                WHERE status = 'pending_review'
                """
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "yellow" if count > 0 else "green",
        "pending_count": count,
        "oldest_at": row[1].isoformat() if row and row[1] else None,
    }


async def _cluster1_deferred_disabled_count() -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM business.cluster1_auto_replies
                WHERE status = 'deferred_disabled'
                  AND created_at >= NOW() - INTERVAL '7 days'
                """
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "yellow" if count > 0 else "green",
        "deferred_count_7d": count,
    }


async def _cluster_stuck_outbound(*, cluster: str) -> dict[str, Any]:
    """Step recipients stuck in 'scheduled' status past threshold for
    this cluster."""
    init_filter = (
        "init.kind = 'self_prospecting'"
        if cluster == "cluster_1"
        else "init.kind = 'partner_demand' AND (init.metadata->>'leg')::int = 2"
    )
    sql = f"""
        SELECT COUNT(*)
        FROM business.channel_campaign_step_recipients m
        JOIN business.channel_campaign_steps step ON step.id = m.channel_campaign_step_id
        JOIN business.channel_campaigns cc ON cc.id = step.channel_campaign_id
        JOIN business.gtm_initiatives init ON init.id = cc.initiative_id
        WHERE m.status = 'scheduled'
          AND step.external_provider_id IS NOT NULL
          AND m.processed_at IS NOT NULL
          AND m.processed_at < NOW() - INTERVAL '6 hours'
          AND {init_filter}
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    count = int(row[0]) if row else 0
    if count >= 50:
        status = "red"
    elif count >= 5:
        status = "yellow"
    else:
        status = "green"
    return {"status": status, "stuck_count": count}


async def _cluster1_recent_auto_replies(limit: int = 20) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT car.id, car.created_at, car.status,
                       car.failure_reason,
                       r.display_name, r.email,
                       i.metadata->>'name' AS initiative_name
                FROM business.cluster1_auto_replies car
                LEFT JOIN business.email_messages em
                       ON em.id = car.inbound_email_message_id
                LEFT JOIN business.recipients r ON r.id = em.recipient_id
                LEFT JOIN business.gtm_initiatives i ON i.id = car.initiative_id
                ORDER BY car.created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "created_at": r[1].isoformat() if r[1] else None,
            "status": r[2],
            "failure_reason": r[3],
            "recipient_display": r[4],
            "recipient_email": r[5],
            "initiative_name": r[6],
        }
        for r in rows or []
    ]


async def _cluster2_active_initiatives(*, now: datetime) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) FROM business.gtm_initiatives
                WHERE kind = 'partner_demand'
                  AND status = 'active'
                  AND (metadata->>'leg')::int = 2
                """
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "green",
        "active_count": count,
    }


async def _cluster2_send_rate_last_hour(*, now: datetime) -> dict[str, Any]:
    """Email_messages sent in last hour belonging to Cluster 2 (Leg-2)
    initiatives. Note: send rate is currently 0 because lead-attach is
    stubbed — surface that fact via the `send_path_wired` field."""
    cutoff = now - timedelta(hours=1)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE em.status = 'sent') AS sent,
                    COUNT(*) FILTER (WHERE em.status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE em.status = 'replied') AS replied,
                    COUNT(*) AS total
                FROM business.email_messages em
                JOIN business.channel_campaign_steps step
                  ON step.id = em.channel_campaign_step_id
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                WHERE em.created_at >= %s
                  AND init.kind = 'partner_demand'
                  AND (init.metadata->>'leg')::int = 2
                """,
                (cutoff,),
            )
            row = await cur.fetchone()
    sent, failed, replied, total = row or (0,) * 4
    status = "yellow" if (failed or 0) > 0 else "green"
    return {
        "status": status,
        "sent": sent or 0,
        "failed": failed or 0,
        "replied": replied or 0,
        "total": total or 0,
        "send_path_wired": False,  # lead-attach stub still in place
        "note": "lead-attach to EmailBison is not yet wired; emails currently fire through operator's manual EB workflow",
    }



async def _heartbeat_status(*, now: datetime) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, started_at, completed_at, duration_ms,
                       failure_reason
                FROM business.cluster3_heartbeat_log
                ORDER BY started_at DESC LIMIT 1
                """
            )
            row = await cur.fetchone()
            await cur.execute(
                """
                SELECT MAX(started_at) FROM business.cluster3_heartbeat_log
                WHERE status = 'pass'
                """
            )
            last_pass_row = await cur.fetchone()

    if row is None:
        return {
            "status": "yellow",
            "message": "No heartbeat runs yet — schedule may not have fired",
            "last_pass": None,
            "last_run": None,
        }

    last_status = row[0]
    last_pass = last_pass_row[0] if last_pass_row else None
    last_pass_age = (now - last_pass).total_seconds() if last_pass else None

    if last_status == "pass" and last_pass_age is not None and last_pass_age < 3600 * 2:
        status = "green"
    elif last_pass_age is not None and last_pass_age < 3600 * 6:
        status = "yellow"
    else:
        status = "red"

    return {
        "status": status,
        "last_run_status": last_status,
        "last_run_started_at": row[1].isoformat() if row[1] else None,
        "last_run_duration_ms": row[3],
        "last_run_failure_reason": row[4],
        "last_pass_at": last_pass.isoformat() if last_pass else None,
        "last_pass_age_seconds": last_pass_age,
    }


async def _dispatch_last_hour(*, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'deferred_capped') AS deferred,
                    COUNT(*) FILTER (WHERE status = 'pending_review') AS pending,
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) AS total
                FROM business.lead_transfers
                WHERE created_at >= %s
                """,
                (cutoff,),
            )
            lt_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'orphaned') AS orphaned,
                    COUNT(*) FILTER (WHERE status = 'dead_letter') AS dead_letter,
                    COUNT(*) FILTER (WHERE status = 'processed') AS processed,
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                    COUNT(*) AS total
                FROM webhook_events
                WHERE provider_slug = 'emailbison'
                  AND created_at >= %s
                """,
                (cutoff,),
            )
            wh_row = await cur.fetchone()

    sent, failed, deferred, pending_review, queued, lt_total = lt_row or (0,) * 6
    orph, dead, proc, wh_pending, wh_total = wh_row or (0,) * 5

    orph_rate = (orph / wh_total) if wh_total else 0
    if failed > 0 or dead > 0 or orph_rate > 0.1:
        status = "red"
    elif pending_review > 0 or orph_rate > 0.05 or queued > 0:
        status = "yellow"
    else:
        status = "green"

    return {
        "status": status,
        "lead_transfers": {
            "sent": sent or 0,
            "failed": failed or 0,
            "deferred_capped": deferred or 0,
            "pending_review": pending_review or 0,
            "queued": queued or 0,
            "total": lt_total or 0,
        },
        "webhook_events": {
            "processed": proc or 0,
            "orphaned": orph or 0,
            "dead_letter": dead or 0,
            "pending": wh_pending or 0,
            "total": wh_total or 0,
            "orphan_rate": round(orph_rate, 4),
        },
    }


async def _stuck_queue(*, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(minutes=5)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*),
                       MIN(queued_at),
                       AVG(EXTRACT(EPOCH FROM (NOW() - queued_at)))::int
                FROM business.lead_transfers
                WHERE status = 'queued' AND queued_at < %s
                """,
                (cutoff,),
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "red" if count > 0 else "green",
        "stuck_count": count,
        "oldest_queued_at": row[1].isoformat() if row and row[1] else None,
        "avg_age_seconds": row[2] if row else None,
    }


async def _unclassified_backlog(*, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) FROM business.email_reply_classifications
                WHERE classification = 'unclassified'
                  AND classified_at < %s
                """,
                (cutoff,),
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "red" if count > 5 else ("yellow" if count > 0 else "green"),
        "unclassified_count": count,
    }


async def _pending_review() -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*),
                       MIN(queued_at)
                FROM business.lead_transfers
                WHERE status = 'pending_review'
                """
            )
            row = await cur.fetchone()
    count = (row and row[0]) or 0
    return {
        "status": "yellow" if count > 0 else "green",
        "pending_count": count,
        "oldest_at": row[1].isoformat() if row and row[1] else None,
    }


async def _reconciliation_status(*, now: datetime) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, started_at, completed_at, duration_ms,
                       eb_replies_scanned, eb_replies_backfilled,
                       failure_reason
                FROM business.cluster3_reconciliation_log
                ORDER BY started_at DESC LIMIT 1
                """
            )
            row = await cur.fetchone()
    if row is None:
        return {"status": "yellow", "message": "no reconciliation runs yet"}

    last_run_at = row[1]
    age_seconds = (now - last_run_at).total_seconds() if last_run_at else None
    if age_seconds and age_seconds > 3600 * 36:
        status = "red"
    elif row[5] and row[5] > 0:
        status = "yellow"
    elif row[0] == "fail":
        status = "red"
    else:
        status = "green"

    return {
        "status": status,
        "last_run_status": row[0],
        "last_run_started_at": last_run_at.isoformat() if last_run_at else None,
        "last_run_age_seconds": age_seconds,
        "duration_ms": row[3],
        "scanned": row[4],
        "backfilled": row[5],
        "failure_reason": row[6],
    }


async def _partner_ledgers(*, now: datetime) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT pc.id, p.id, p.name,
                       pc.amount_cents, pc.duration_days,
                       pc.starts_at, pc.ends_at, pc.status,
                       prop.proposed_transfer_count,
                       prop.final_data_engine_audience_id,
                       prop.proposed_data_engine_audience_id,
                       (
                         SELECT COUNT(*) FROM business.lead_transfers lt
                         WHERE lt.partner_contract_id = pc.id
                           AND lt.status = 'sent'
                       ) AS delivered_sent,
                       (
                         SELECT COUNT(*) FROM business.lead_transfers lt
                         WHERE lt.partner_contract_id = pc.id
                           AND lt.status = 'queued'
                       ) AS in_flight
                FROM business.partner_contracts pc
                JOIN business.demand_side_partners p ON p.id = pc.partner_id
                LEFT JOIN business.proposals prop
                  ON prop.partner_contract_id = pc.id AND prop.status = 'paid'
                WHERE pc.status IN ('active', 'draft')
                ORDER BY pc.created_at DESC
                LIMIT 50
                """
            )
            rows = await cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows or []:
        ends_at = r[6]
        days_remaining = None
        if ends_at:
            secs = (ends_at - now).total_seconds()
            days_remaining = round(secs / 86400, 1)
        paid_count = r[8]
        delivered = r[11] or 0
        remaining = (paid_count - delivered) if paid_count is not None else None
        utilization = (delivered / paid_count) if paid_count else None
        status_color = (
            "red"
            if (
                paid_count is not None
                and remaining is not None
                and remaining > 0
                and days_remaining is not None
                and days_remaining < 7
                and utilization is not None
                and utilization < 0.5
            )
            else (
                "yellow"
                if (
                    days_remaining is not None
                    and days_remaining < 14
                    and utilization is not None
                    and utilization < 0.8
                )
                else "green"
            )
        )
        out.append(
            {
                "partner_contract_id": str(r[0]),
                "partner_id": str(r[1]),
                "partner_name": r[2],
                "amount_cents": r[3],
                "duration_days": r[4],
                "contract_status": r[7],
                "starts_at": r[5].isoformat() if r[5] else None,
                "ends_at": ends_at.isoformat() if ends_at else None,
                "days_remaining": days_remaining,
                "paid_transfer_count": paid_count,
                "delivered_count": delivered,
                "in_flight_count": r[12] or 0,
                "remaining_capacity": remaining,
                "utilization": round(utilization, 3) if utilization is not None else None,
                "status": status_color,
            }
        )
    return out


async def _source_material_freshness(*, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(days=30)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM business.demand_side_partners p
                     WHERE p.deleted_at IS NULL
                       AND NOT EXISTS (
                         SELECT 1 FROM business.partner_research_artifacts pra
                         WHERE pra.partner_id = p.id
                           AND pra.updated_at >= %s
                       )) AS partners_missing_or_stale,
                    (SELECT COUNT(*) FROM business.demand_side_partners p
                     WHERE p.deleted_at IS NULL
                       AND NOT EXISTS (
                         SELECT 1 FROM business.partner_research_artifacts pra
                         WHERE pra.partner_id = p.id
                       )) AS partners_missing
                """,
                (cutoff,),
            )
            row = await cur.fetchone()

    pmoss, pm = row or (0, 0)
    if pm and pm > 0:
        status = "red"
    elif pmoss and pmoss > 0:
        status = "yellow"
    else:
        status = "green"
    return {
        "status": status,
        "partners_missing_research_artifact": pm or 0,
        "partners_missing_or_stale_research_artifact": pmoss or 0,
        "stale_threshold_days": 30,
    }


async def _recent_alerts(limit: int = 20) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, fired_at, severity, source, summary
                FROM business.cluster3_alerts
                ORDER BY fired_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "fired_at": r[1].isoformat(),
            "severity": r[2],
            "source": r[3],
            "summary": r[4],
        }
        for r in rows or []
    ]


async def _recent_dispatches(limit: int = 20) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT lt.id, lt.created_at, lt.status,
                       lt.partner_id, p.name,
                       lt.recipient_id,
                       lt.failure_reason,
                       lt.allocation_snapshot
                FROM business.lead_transfers lt
                LEFT JOIN business.demand_side_partners p ON p.id = lt.partner_id
                ORDER BY lt.created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "created_at": r[1].isoformat() if r[1] else None,
            "status": r[2],
            "partner_id": str(r[3]) if r[3] else None,
            "partner_name": r[4],
            "recipient_id": str(r[5]) if r[5] else None,
            "failure_reason": r[6],
            "allocation_snapshot": r[7] or {},
        }
        for r in rows or []
    ]


__all__ = ["snapshot", "overall_snapshot"]
