"""POST /internal/sba-bridges/run-daily — SBA/PDL/SAM/USAspending bridge cron.

Triggered by the Trigger.dev task ``sba-bridges-daily`` at 09:00 UTC.
Forwards to DEX's /api/internal/sba-bridges/run-daily endpoint which
subprocesses the 7 daily-refresh scripts in sequence.

Auth: TRIGGER_SHARED_SECRET (same pattern as all internal Trigger.dev
endpoints in hq-x — see dmaas_jobs.py, scheduler.py, etc.)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends

from app.auth.trigger_secret import verify_trigger_secret
from app.services import dex_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sba-bridges", tags=["internal"])


@router.post("/run-daily", dependencies=[Depends(verify_trigger_secret)])
async def run_daily(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Forward the daily bridge-run request to DEX.

    DEX subprocesses the 7 Lance emit + bridge scripts in its own
    Doppler environment. The trigger_run_id is passed through for
    end-to-end traceability in ops.bridge_generation_runs.
    """
    return await dex_client._request(
        "POST",
        "/api/internal/sba-bridges/run-daily",
        bearer_token=None,  # falls back to DEX_SERVICE_TOKEN
        json=payload,
    )


__all__ = ["router"]
