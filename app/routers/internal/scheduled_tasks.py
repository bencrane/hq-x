"""Internal gate endpoint for the scheduled-task control plane.

Every Trigger.dev scheduled task calls POST /internal/scheduled-tasks/gate with
its own ``{task_id}`` before doing work. hq-x reads ops.scheduled_tasks.is_enabled,
stamps the fire-ledger (last_gate_check_at), and answers whether to run.

The TS side (src/trigger/lib/scheduled-gate.ts) is FAIL-OPEN: any error talking
to this endpoint defaults to running, so an hq-x blip never silently halts an
SLA-critical schedule. This endpoint therefore only needs to be correct, not
defensive — an unknown task_id returns run=true (see services.scheduled_tasks.gate).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.services import scheduled_tasks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduled-tasks", tags=["internal"])


@router.post("/gate", dependencies=[Depends(verify_trigger_secret)])
async def scheduled_task_gate(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "task_id required"},
        )
    decision = await scheduled_tasks.gate(task_id)
    if not decision["run"]:
        logger.info("scheduled-task gate: %s disabled — instructing skip", task_id)
    return decision
