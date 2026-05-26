"""Read-only proxy for the DEX GTM cohort endpoints.

Forwards inbound calls (authed with BACKEND_X_SERVICE_TOKEN — same shared
secret the Trigger.dev orchestrator uses for `callHqxApi`) to DEX
`GET /api/v1/gtm/cohorts/primes-90d/{lane}` over DEX_SERVICE_TOKEN. DEX
owns the Lance scan off the pre-materialized cohorts/* datasets.

Endpoint:
  GET /api/v1/gtm/cohorts/primes-90d/{lane}    lane ∈ {"fast", "slow"}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/gtm/cohorts", tags=["gtm-cohorts"])


@router.get("/primes-90d/{lane}")
async def get_primes_90d_cohort(
    lane: str,
    _auth: None = Depends(verify_backend_x_token),
) -> list[dict[str, Any]]:
    if lane not in ("fast", "slow"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "unknown_cohort_lane",
                "lane": lane,
                "allowed": ["fast", "slow"],
            },
        )
    try:
        payload = await dex_client.get_cohort_primes_90d(lane=lane)
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc

    if not isinstance(payload, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "dex_payload_unexpected",
                "message": "DEX cohort payload was not a list",
            },
        )
    return payload
