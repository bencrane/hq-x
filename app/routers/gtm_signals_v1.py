"""Read-only passthrough to DEX /api/v1/gtm/signals for the platform-api BFF.

Mirrors the coverage_stats_v1 / gtm_views passthrough pattern: hq-zone-api
hits us with BACKEND_X_SERVICE_TOKEN; we forward to DEX with DEX_SERVICE_TOKEN
from this app's Doppler config. DEX owns the actual ops.gtm_signals data.

Endpoint:
  GET /api/v1/signals
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/signals", tags=["gtm-signals"])


@router.get("")
async def list_gtm_signals(
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.list_gtm_signals()
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
