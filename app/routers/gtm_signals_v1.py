"""Read + edit passthrough to DEX /api/v1/gtm/signals for the platform-api BFF.

Mirrors the coverage_stats_v1 / gtm_views passthrough pattern: hq-zone-api
hits us with BACKEND_X_SERVICE_TOKEN; we forward to DEX with DEX_SERVICE_TOKEN
from this app's Doppler config. DEX owns the actual ops.gtm_signals data.

Endpoints:
  GET    /api/v1/signals          → list
  PATCH  /api/v1/signals/{slug}   → patch webhook URLs / webhook_target / is_active
  DELETE /api/v1/signals/{slug}   → hard-delete a signal row
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/signals", tags=["gtm-signals"])


class SignalPatchBody(BaseModel):
    """Mirrors DEX's SignalPatchRequest. Pydantic enforces type + pattern;
    extra fields rejected so a typo doesn't silently no-op."""
    webhook_test_url: str | None = Field(default=None, max_length=2000)
    webhook_prod_url: str | None = Field(default=None, max_length=2000)
    webhook_target:   str | None = Field(default=None, pattern=r"^(test|prod)$")
    is_active:        bool | None = None
    model_config = ConfigDict(extra="forbid")


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


@router.patch("/{signal_slug}")
async def patch_gtm_signal(
    signal_slug: str,
    payload: SignalPatchBody,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    patch = payload.model_dump(exclude_none=True)
    try:
        return await dex_client.patch_gtm_signal(signal_slug, patch)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"type": "not_found", "message": f"signal {signal_slug!r} not found"},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc


@router.delete("/{signal_slug}")
async def delete_gtm_signal(
    signal_slug: str,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.delete_gtm_signal(signal_slug)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"type": "not_found", "message": f"signal {signal_slug!r} not found"},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
