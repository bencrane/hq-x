"""Internal Cluster 1 / 2 / 3 endpoints — invoked by Trigger.dev schedules.

Auth: Trigger shared-secret bearer (mirrors other internal/* routers).
Endpoints:

    POST /internal/cluster3/heartbeat
        Run one Cluster 3 synthetic heartbeat (intro dispatch).

    POST /internal/cluster3/recovery-sweep
        Find stuck queued lead_transfers (Cluster 3), retry or fail.

    POST /internal/cluster3/reconciliation-sweep
        Poll EB for replies we missed; backfill events; trigger orchestrator.

    POST /internal/cluster1/heartbeat
        Run one Cluster 1 outbound heartbeat (synthetic activate-step).

    POST /internal/cluster2/heartbeat
        Run one Cluster 2 outbound heartbeat (synthetic activate-step).

    POST /internal/clusters/outbound-recovery-sweep
        Cluster 1 + 2 stuck step recipients (status='scheduled' too long).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.trigger_secret import verify_trigger_secret
from app.services import (
    cluster3_heartbeat,
    cluster3_reconciliation,
    cluster3_recovery,
    cluster_outbound_heartbeat,
    cluster_outbound_recovery,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cluster3", tags=["internal"])


@router.post(
    "/heartbeat",
    dependencies=[Depends(verify_trigger_secret)],
)
async def heartbeat() -> dict[str, Any]:
    result = await cluster3_heartbeat.run_heartbeat()
    staleness = await cluster3_heartbeat.staleness_check()
    return {"heartbeat": result, "staleness": staleness}


@router.post(
    "/recovery-sweep",
    dependencies=[Depends(verify_trigger_secret)],
)
async def recovery_sweep() -> dict[str, Any]:
    return await cluster3_recovery.sweep_stuck_queued()


@router.post(
    "/reconciliation-sweep",
    dependencies=[Depends(verify_trigger_secret)],
)
async def reconciliation_sweep(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    return await cluster3_reconciliation.sweep_reconciliation(
        lookback_hours=int(body.get("lookback_hours", 48)),
        per_campaign_limit=int(body.get("per_campaign_limit", 200)),
    )


# Sibling router for cross-cluster outbound endpoints. Uses a separate
# APIRouter object so we can mount it under /internal without colliding
# with the /cluster3 prefix.
clusters_router = APIRouter(prefix="/clusters", tags=["internal"])


@clusters_router.post(
    "/cluster1/heartbeat",
    dependencies=[Depends(verify_trigger_secret)],
)
async def cluster1_heartbeat() -> dict[str, Any]:
    result = await cluster_outbound_heartbeat.run_outbound_heartbeat(
        cluster="cluster_1"
    )
    staleness = await cluster_outbound_heartbeat.staleness_check(cluster="cluster_1")
    return {"heartbeat": result, "staleness": staleness}


@clusters_router.post(
    "/cluster2/heartbeat",
    dependencies=[Depends(verify_trigger_secret)],
)
async def cluster2_heartbeat() -> dict[str, Any]:
    result = await cluster_outbound_heartbeat.run_outbound_heartbeat(
        cluster="cluster_2"
    )
    staleness = await cluster_outbound_heartbeat.staleness_check(cluster="cluster_2")
    return {"heartbeat": result, "staleness": staleness}


@clusters_router.post(
    "/outbound-recovery-sweep",
    dependencies=[Depends(verify_trigger_secret)],
)
async def outbound_recovery_sweep() -> dict[str, Any]:
    return await cluster_outbound_recovery.sweep()


__all__ = ["router", "clusters_router"]
