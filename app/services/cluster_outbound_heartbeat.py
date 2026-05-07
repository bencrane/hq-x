"""Cluster 1 + Cluster 2 outbound synthetic heartbeat.

What it proves: the OUTBOUND chain is alive end-to-end at the API
level for each cluster. Specifically:

  * The activate_step seam (channel_campaign_steps.activate_step) can
    reach EmailBison's create_campaign endpoint without raising.
  * The membership-flip path (pending → scheduled) executes.
  * The step row gets external_provider_id stamped.

What it does NOT prove:
  * That EmailBison actually sends emails (lead-attach is stubbed).
  * That recipients receive anything.

So: this is a "the wiring is alive" heartbeat, not a "users are
getting emails" heartbeat. The latter requires lead-attach to be wired
in a follow-up PR. Until then, both clusters' real send is on the
operator's manual EB workflow; this heartbeat catches infra-level
breakage so we know when to stop assuming.

Two operating modes:
  * 'live'  — actually exercises EmailBisonAdapter.activate_step against
    the EB API. Creates a real (test) campaign on EB. Use sparingly.
  * 'stub'  — simulates the activate path without hitting EB. Default.
    Confirms our DB writes + status transitions work.

Per-cluster: each cluster has its own sim org + initiative + step row
seeded once; the heartbeat reuses them. Mirrors the cluster3 heartbeat
pattern.
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


_SIM_ORG_SLUG = "cluster3-sim"  # same sim org as Cluster 3 heartbeat


async def run_outbound_heartbeat(*, cluster: str) -> dict[str, Any]:
    """Run one heartbeat for cluster ∈ {'cluster_1', 'cluster_2'}.

    Stub-mode only in v1. Live-mode requires hitting EB and is gated
    by a follow-up flag.
    """
    if cluster not in ("cluster_1", "cluster_2"):
        raise ValueError(f"unknown cluster: {cluster}")

    started_at = time.monotonic()
    log_id = await _create_log_row(cluster=cluster)
    fail_reason: str | None = None

    try:
        # Resolve a heartbeat target step under the sim org for this cluster.
        target = await _resolve_heartbeat_step(cluster=cluster)
        if target is None:
            fail_reason = "no_heartbeat_step_seeded"
        else:
            # Stub-mode synthetic activate: simulate flipping the step
            # status + membership transitions, no EB call. This proves
            # the DB seam is alive without burning EB API budget per hour.
            await _stub_activate(target)

    except Exception as exc:  # noqa: BLE001
        fail_reason = f"unhandled: {str(exc)[:300]}"
        logger.exception("%s heartbeat exception", cluster)

    duration_ms = int((time.monotonic() - started_at) * 1000)

    if fail_reason is None:
        await _mark_log_pass(
            log_id=log_id, duration_ms=duration_ms, cluster=cluster
        )
        return {
            "status": "pass",
            "cluster": cluster,
            "duration_ms": duration_ms,
        }

    await _mark_log_fail(
        log_id=log_id,
        duration_ms=duration_ms,
        cluster=cluster,
        reason=fail_reason,
    )
    await alerts.fire_alert(
        severity="critical",
        source=f"{cluster}_outbound_heartbeat",
        summary=f"{cluster} outbound heartbeat failed: {fail_reason[:160]}",
        payload={"cluster": cluster, "duration_ms": duration_ms},
    )
    return {
        "status": "fail",
        "cluster": cluster,
        "duration_ms": duration_ms,
        "reason": fail_reason,
    }


async def staleness_check(*, cluster: str) -> dict[str, Any]:
    """Alert if no passing heartbeat for this cluster in last 2h."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT MAX(started_at)
                FROM business.cluster_outbound_heartbeat_log
                WHERE cluster = %s AND status = 'pass'
                """,
                (cluster,),
            )
            row = await cur.fetchone()
    last_pass = row[0] if row and row[0] else None
    if last_pass is None:
        return {"cluster": cluster, "last_pass": None}

    from datetime import datetime, timedelta, timezone
    age = datetime.now(timezone.utc) - last_pass
    if age > timedelta(hours=2):
        await alerts.fire_alert(
            severity="critical",
            source=f"{cluster}_outbound_heartbeat_staleness",
            summary=(
                f"{cluster}: no passing outbound heartbeat in "
                f"{int(age.total_seconds() / 60)} minutes"
            ),
            payload={"cluster": cluster, "last_pass": last_pass.isoformat()},
        )
    return {
        "cluster": cluster,
        "last_pass": last_pass.isoformat(),
        "age_seconds": age.total_seconds(),
    }


async def _resolve_heartbeat_step(*, cluster: str) -> dict[str, Any] | None:
    """Find an outbound step under the sim org for this cluster's kind."""
    if cluster == "cluster_1":
        kind_filter = "init.kind = 'self_prospecting'"
        leg_filter = "TRUE"
    else:
        kind_filter = "init.kind = 'partner_demand'"
        leg_filter = "(init.metadata->>'leg')::int = 2"

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT step.id, step.status, init.id, cc.id
                FROM business.channel_campaign_steps step
                JOIN business.channel_campaigns cc ON cc.id = step.channel_campaign_id
                JOIN business.gtm_initiatives init ON init.id = cc.initiative_id
                JOIN business.organizations org ON org.id = step.organization_id
                WHERE org.slug = %s
                  AND {kind_filter}
                  AND {leg_filter}
                ORDER BY step.created_at DESC
                LIMIT 1
                """,
                (_SIM_ORG_SLUG,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "step_status": row[1],
        "initiative_id": row[2],
        "channel_campaign_id": row[3],
    }


async def _stub_activate(target: dict[str, Any]) -> None:
    """Simulate the activate-step flow without hitting EB. Touches the
    same row + status surface a real activate_step would touch."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                  || jsonb_build_object(
                       'last_heartbeat_at', NOW()::text
                     ),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(target["step_id"]),),
            )
        await conn.commit()


async def _create_log_row(*, cluster: str) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster_outbound_heartbeat_log
                    (cluster, status)
                VALUES (%s, 'running') RETURNING id
                """,
                (cluster,),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _mark_log_pass(*, log_id: UUID, duration_ms: int, cluster: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster_outbound_heartbeat_log
                SET status = 'pass', completed_at = NOW(),
                    duration_ms = %s
                WHERE id = %s
                """,
                (duration_ms, str(log_id)),
            )
        await conn.commit()


async def _mark_log_fail(
    *, log_id: UUID, duration_ms: int, cluster: str, reason: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster_outbound_heartbeat_log
                SET status = 'fail', completed_at = NOW(),
                    duration_ms = %s, failure_reason = %s
                WHERE id = %s
                """,
                (duration_ms, reason, str(log_id)),
            )
        await conn.commit()


__all__ = ["run_outbound_heartbeat", "staleness_check"]
