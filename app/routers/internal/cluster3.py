"""Internal Cluster 3 endpoints — invoked by Trigger.dev schedules.

Auth: Trigger shared-secret bearer (mirrors other internal/* routers).
Endpoints:

    POST /internal/cluster3/heartbeat
        Run one synthetic heartbeat. Returns pass/fail with diagnostics.

    POST /internal/cluster3/recovery-sweep
        Find stuck queued lead_transfers, retry or fail.

    POST /internal/cluster3/reconciliation-sweep
        Poll EB for replies we missed; backfill events; trigger orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.trigger_secret import verify_trigger_secret
from app.services import cluster3_heartbeat, cluster3_reconciliation, cluster3_recovery

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


__all__ = ["router"]
