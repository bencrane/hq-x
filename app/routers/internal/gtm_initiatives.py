"""Internal endpoint that drives a gtm-initiative strategy synthesis to
terminal state.

The Trigger.dev task ``gtm.synthesize_initiative_strategy`` POSTs here
with ``{initiative_id, trigger_run_id}``. We:

1. Refuse if the initiative is already in a terminal slice-1 state
   (``strategy_ready``, ``failed``, ``cancelled``) — return 200 with
   ``{skipped: true}`` so Trigger doesn't retry.
2. Hand off to ``strategy_synthesizer.synthesize_initiative_strategy``
   which does the Anthropic call + disk write + state transition.
3. Surface any synthesizer-level failure so Trigger's retry policy can
   pick it up (transient Anthropic 5xx). Deterministic two-strikes
   YAML failures are not re-raised — the synthesizer already
   transitioned the initiative to ``failed`` and persisted the bad
   output for inspection.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.services import gtm_initiatives as gtm_svc
from app.services import strategy_synthesizer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/initiatives", tags=["internal"])


_TERMINAL_STATES = {
    "strategy_ready",
    "materializing",
    "ready_to_launch",
    "active",
    "completed",
    "cancelled",
}


@router.post(
    "/{initiative_id}/process-synthesis",
    dependencies=[Depends(verify_trigger_secret)],
)
async def process_synthesis(
    initiative_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "initiative_not_found"},
        )

    if initiative["status"] in _TERMINAL_STATES:
        return {
            "initiative_id": str(initiative_id),
            "status": initiative["status"],
            "skipped": True,
            "reason": "initiative_already_terminal",
            "campaign_strategy_path": initiative.get("campaign_strategy_path"),
        }

    try:
        result = await strategy_synthesizer.synthesize_initiative_strategy(
            initiative_id=initiative_id,
            organization_id=initiative["organization_id"],
        )
    except strategy_synthesizer.StrategySynthesizerError as exc:
        # Two-strikes YAML failure path. The synthesizer already
        # transitioned the initiative to `failed` and persisted the
        # raw output; surface 200 so Trigger doesn't retry the same
        # deterministic error.
        return {
            "initiative_id": str(initiative_id),
            "status": "failed",
            "error": str(exc)[:500],
        }

    return {
        "initiative_id": str(initiative_id),
        "status": "succeeded",
        "path": result["path"],
        "model": result.get("model"),
        "tokens_used": result.get("tokens_used"),
        "cache_read_input_tokens": result.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": result.get("cache_creation_input_tokens"),
    }


__all__ = ["router"]
