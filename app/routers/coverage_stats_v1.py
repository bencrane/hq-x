"""Read-only passthrough to DEX /coverage/stats for the hq-zone platform-api BFF.

Thin proxy: hq-zone-api hits us with BACKEND_X_SERVICE_TOKEN; we
forward to DEX `GET /coverage/stats` using DEX_SERVICE_TOKEN from
this app's Doppler config. DEX owns the actual coverage_stats data.

Per-user scoping is intentionally omitted — coverage data is
operator-grade meta-stats (datasets / bridges / intersections).

Endpoint:
  GET /api/v1/coverage/stats
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/coverage/stats", tags=["coverage-stats"])


@router.get("")
async def get_coverage_stats(
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.list_coverage_stats()
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
