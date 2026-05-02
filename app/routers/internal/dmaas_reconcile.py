"""Internal reconciliation endpoints driven by Trigger.dev cron tasks.

Each endpoint dispatches to a feature-flag-gated reconciler in
``app.services.reconciliation``. Returns the structured
``ReconciliationResult`` so the operator dashboard / Trigger.dev run
log can show counts (rows scanned, rows touched, drift found) plus
an inspectable details list.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.trigger_secret import verify_trigger_secret
from app.services.reconciliation import (
    customer_webhook_deliveries as r_cw,
)
from app.services.reconciliation import (
    dub_clicks as r_dub,
)
from app.services.reconciliation import (
    lob_pieces as r_lob,
)
from app.services.reconciliation import (
    stale_jobs as r_stale,
)
from app.services.reconciliation import (
    webhook_replays as r_wh,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dmaas/reconcile", tags=["internal"])


def _result_to_dict(result: Any) -> dict[str, Any]:
    return result.model_dump()


@router.post("/stale-jobs", dependencies=[Depends(verify_trigger_secret)])
async def reconcile_stale_jobs() -> dict[str, Any]:
    return _result_to_dict(await r_stale.reconcile())


@router.post("/lob", dependencies=[Depends(verify_trigger_secret)])
async def reconcile_lob() -> dict[str, Any]:
    return _result_to_dict(await r_lob.reconcile())


@router.post("/dub", dependencies=[Depends(verify_trigger_secret)])
async def reconcile_dub() -> dict[str, Any]:
    return _result_to_dict(await r_dub.reconcile())


@router.post("/webhook-replays", dependencies=[Depends(verify_trigger_secret)])
async def reconcile_webhook_replays() -> dict[str, Any]:
    return _result_to_dict(await r_wh.reconcile())


@router.post(
    "/customer-webhook-deliveries", dependencies=[Depends(verify_trigger_secret)]
)
async def reconcile_customer_webhook_deliveries() -> dict[str, Any]:
    return _result_to_dict(await r_cw.reconcile())


__all__ = ["router"]
