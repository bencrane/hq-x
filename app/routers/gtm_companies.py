"""Read-only proxy for the DEX GTM-companies hydration slice.

Thin proxy: hq-zone-api (and the Trigger.dev orchestrator) hit us with
BACKEND_X_SERVICE_TOKEN; we forward to DEX `GET /api/v1/gtm/companies/
hydration-slice` using DEX_SERVICE_TOKEN from this app's Doppler config. DEX
owns the actual Lance/DuckDB extraction off the SAM ↔ PDL ↔ USAspending
bridge.

Per-user scoping is intentionally omitted — gtm data is operator-grade in
single-operator world.

Endpoint:
  GET /api/v1/gtm/companies/hydration-slice
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/gtm/companies", tags=["gtm-companies"])


@router.get("/hydration-slice")
async def get_hydration_slice(
    _auth: None = Depends(verify_backend_x_token),
) -> list[dict[str, Any]]:
    try:
        payload = await dex_client.get_companies_hydration_slice()
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
                "message": "DEX hydration-slice payload was not a list",
            },
        )
    return payload
