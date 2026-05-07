"""Cluster 1 + Cluster 2 outbound stuck-step recovery sweep.

Two failure surfaces this catches:

  1. activation_jobs stuck in 'queued'/'running' past threshold.
     The dmaas-reconcile-stale-jobs sweep already handles this case
     (transitions stale jobs to 'failed'); we re-export its behavior
     here for symmetry but mostly emit telemetry / alerts.

  2. channel_campaign_step_recipients stuck in 'scheduled' (i.e. the
     step activated and we flipped membership to scheduled, but the
     EB campaign never moved leads to sent). This case is NOT covered
     by stale_jobs because the activation job succeeded — the failure
     is downstream at the EB lead-attach + provider-send seam.

For case 2: we don't have a deterministic 'this should have sent by
now' clock built in (EB doesn't expose it). We use a heuristic:
membership stuck in 'scheduled' for > N hours, where the parent step's
external_provider_id (EB campaign id) is non-null. Two outcomes:

  * count > 0 → fire warning alert (operator should look)
  * count > threshold → fire critical alert + log row

This is observability, not auto-recovery — automatic 're-attach leads
to EB' would be wrong without operator review of why the original
attach didn't fire (which today is because lead-attach isn't even
wired; that's a separate PR).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.services import alerts

logger = logging.getLogger(__name__)


SCHEDULED_STUCK_HOURS = 6
SCHEDULED_STUCK_THRESHOLD_CRITICAL = 50  # absolute count of stuck rows
SCHEDULED_STUCK_THRESHOLD_WARNING = 5


async def sweep() -> dict[str, Any]:
    started_at = time.monotonic()
    log_id = await _create_log_row()

    try:
        # Cluster 1 + Cluster 2 share the same step_recipients table; we
        # discriminate via the step → channel_campaign → initiative.kind
        # join. Single sweep for both, separate stats per cluster.
        per_cluster: dict[str, dict[str, Any]] = {}
        for cluster_kind, init_filter in [
            ("cluster_1", "init.kind = 'self_prospecting'"),
            (
                "cluster_2",
                "init.kind = 'partner_demand' "
                "AND (init.metadata->>'leg')::int = 2",
            ),
        ]:
            count = await _count_scheduled_stuck(init_filter)
            per_cluster[cluster_kind] = {"scheduled_stuck_count": count}

            if count >= SCHEDULED_STUCK_THRESHOLD_CRITICAL:
                await alerts.fire_alert(
                    severity="critical",
                    source=f"{cluster_kind}_outbound_recovery",
                    summary=(
                        f"{cluster_kind}: {count} step recipients stuck in "
                        f"'scheduled' >{SCHEDULED_STUCK_HOURS}h"
                    ),
                    payload={"cluster": cluster_kind, "stuck_count": count},
                )
            elif count >= SCHEDULED_STUCK_THRESHOLD_WARNING:
                await alerts.fire_alert(
                    severity="warning",
                    source=f"{cluster_kind}_outbound_recovery",
                    summary=(
                        f"{cluster_kind}: {count} step recipients stuck in "
                        f"'scheduled' >{SCHEDULED_STUCK_HOURS}h"
                    ),
                    payload={"cluster": cluster_kind, "stuck_count": count},
                )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _mark_log_pass(log_id=log_id, duration_ms=duration_ms, per_cluster=per_cluster)
        return {
            "status": "pass",
            "duration_ms": duration_ms,
            "per_cluster": per_cluster,
        }
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _mark_log_fail(
            log_id=log_id, duration_ms=duration_ms, reason=str(exc)[:300]
        )
        await alerts.fire_alert(
            severity="critical",
            source="cluster_outbound_recovery",
            summary=f"Recovery sweep crashed: {str(exc)[:160]}",
            payload={"error": str(exc)[:500]},
        )
        raise


async def _count_scheduled_stuck(init_filter: str) -> int:
    sql = f"""
        SELECT COUNT(*)
        FROM business.channel_campaign_step_recipients m
        JOIN business.channel_campaign_steps step ON step.id = m.channel_campaign_step_id
        JOIN business.channel_campaigns cc ON cc.id = step.channel_campaign_id
        JOIN business.gtm_initiatives init ON init.id = cc.initiative_id
        WHERE m.status = 'scheduled'
          AND step.external_provider_id IS NOT NULL
          AND m.processed_at IS NOT NULL
          AND m.processed_at < NOW() - INTERVAL '{SCHEDULED_STUCK_HOURS} hours'
          AND {init_filter}
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _create_log_row() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster_step_recovery_log (status)
                VALUES ('running') RETURNING id
                """
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _mark_log_pass(
    *, log_id: UUID, duration_ms: int, per_cluster: dict[str, Any]
) -> None:
    candidates = sum(
        v.get("scheduled_stuck_count", 0) for v in per_cluster.values()
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster_step_recovery_log
                SET status = 'pass', completed_at = NOW(),
                    duration_ms = %s,
                    candidates_found = %s,
                    metadata = %s
                WHERE id = %s
                """,
                (
                    duration_ms,
                    candidates,
                    Jsonb({"per_cluster": per_cluster}),
                    str(log_id),
                ),
            )
        await conn.commit()


async def _mark_log_fail(
    *, log_id: UUID, duration_ms: int, reason: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster_step_recovery_log
                SET status = 'fail', completed_at = NOW(),
                    duration_ms = %s, failure_reason = %s
                WHERE id = %s
                """,
                (duration_ms, reason, str(log_id)),
            )
        await conn.commit()


__all__ = ["sweep"]
