"""Passthrough to DEX `/api/v1/gtm/audiences` for the hq-zone platform-api BFF.

Thin proxy: hq-zone-api hits us with BACKEND_X_SERVICE_TOKEN; we forward
to DEX with DEX_SERVICE_TOKEN from this app's Doppler config. DEX owns
the `gtm.audiences` table and the spec→DuckDB compiler.

Operator-grade; no per-user scoping (single-operator model).

Endpoints:
  GET    /api/v1/gtm/audiences                 → list
  POST   /api/v1/gtm/audiences                 → create
  GET    /api/v1/gtm/audiences/{id}            → get one
  PATCH  /api/v1/gtm/audiences/{id}            → patch
  DELETE /api/v1/gtm/audiences/{id}            → delete
  POST   /api/v1/gtm/audiences/{id}/compute    → compile + run + persist
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/gtm/audiences", tags=["gtm-audiences"])


# Shape-only validation; DEX runs the full semantic validation (known
# source_id, valid field/operator combinations) and returns 400 if the
# spec is invalid.
class _AudienceSource(BaseModel):
    source_id: str = Field(..., min_length=1)
    model_config = ConfigDict(extra="forbid")


class _AudienceCriterion(BaseModel):
    field: str = Field(..., min_length=1)
    operator: str = Field(..., min_length=1)
    value: Any | None = None
    model_config = ConfigDict(extra="forbid")


class AudienceSpecRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    entity_grain: str = Field(..., min_length=1)
    sources: list[_AudienceSource] = Field(..., min_length=1)
    criteria: list[_AudienceCriterion] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class AudiencePatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    entity_grain: str | None = None
    sources: list[_AudienceSource] | None = Field(default=None, min_length=1)
    criteria: list[_AudienceCriterion] | None = None
    model_config = ConfigDict(extra="forbid")


def _proxy_dex_error(exc: dex_client.DexCallError) -> HTTPException:
    """Pass DEX 4xx through verbatim; 5xx becomes 502."""
    if 400 <= exc.status_code < 500:
        return HTTPException(status_code=exc.status_code, detail=exc.body)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"type": "dex_call_failed", "message": str(exc), "body": exc.body},
    )


@router.get("")
async def list_endpoint(_auth: None = Depends(verify_backend_x_token)) -> dict[str, Any]:
    try:
        return await dex_client.list_gtm_audiences()
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: AudienceSpecRequest,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.create_gtm_audience(payload.model_dump())
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.get("/{audience_id}")
async def get_endpoint(
    audience_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.get_gtm_audience(audience_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.patch("/{audience_id}")
async def patch_endpoint(
    audience_id: UUID,
    payload: AudiencePatchRequest,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    patch = payload.model_dump(exclude_none=True)
    try:
        return await dex_client.patch_gtm_audience(audience_id, patch)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.delete("/{audience_id}")
async def delete_endpoint(
    audience_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.delete_gtm_audience(audience_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("/{audience_id}/compute")
async def compute_endpoint(
    audience_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.compute_gtm_audience(audience_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc
