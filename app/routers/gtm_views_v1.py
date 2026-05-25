"""Passthrough to DEX `/api/v1/gtm/views` for the hq-zone platform-api BFF.

Thin proxy: hq-zone-api hits us with BACKEND_X_SERVICE_TOKEN; we forward
to DEX with DEX_SERVICE_TOKEN from this app's Doppler config. DEX owns
the `gtm.views` table, the Polaris-driven source catalog, and the
compute + materialize pipelines.

Operator-grade; no per-user scoping (single-operator model).

Endpoints:
  GET    /api/v1/gtm/views                       → list
  POST   /api/v1/gtm/views                       → create
  GET    /api/v1/gtm/views/{id}                  → get one
  PATCH  /api/v1/gtm/views/{id}                  → patch
  DELETE /api/v1/gtm/views/{id}                  → delete
  POST   /api/v1/gtm/views/{id}/compute          → stateless count
  POST   /api/v1/gtm/views/{id}/materialize      → emit Lance + register
  GET    /api/v1/gtm/views/catalog/sources       → Polaris-driven catalog
  POST   /api/v1/gtm/views/catalog/refresh       → force-refresh cache
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/gtm/views", tags=["gtm-views"])


class _ViewSource(BaseModel):
    source_id: str = Field(..., min_length=1)
    model_config = ConfigDict(extra="forbid")


class _ViewCriterion(BaseModel):
    field: str = Field(..., min_length=1)
    operator: str = Field(..., min_length=1)
    value: Any | None = None
    model_config = ConfigDict(extra="forbid")


class ViewSpecRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    entity_grain: str = Field(..., min_length=1)
    sources: list[_ViewSource] = Field(..., min_length=1)
    criteria: list[_ViewCriterion] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class ViewPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    entity_grain: str | None = None
    sources: list[_ViewSource] | None = Field(default=None, min_length=1)
    criteria: list[_ViewCriterion] | None = None
    model_config = ConfigDict(extra="forbid")


def _proxy_dex_error(exc: dex_client.DexCallError) -> HTTPException:
    """Pass DEX 4xx through verbatim; 5xx becomes 502."""
    if 400 <= exc.status_code < 500:
        return HTTPException(status_code=exc.status_code, detail=exc.body)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"type": "dex_call_failed", "message": str(exc), "body": exc.body},
    )


@router.get("/catalog/sources")
async def catalog_sources_endpoint(_auth: None = Depends(verify_backend_x_token)) -> dict[str, Any]:
    try:
        return await dex_client.list_gtm_view_sources()
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("/catalog/refresh")
async def catalog_refresh_endpoint(_auth: None = Depends(verify_backend_x_token)) -> dict[str, Any]:
    try:
        return await dex_client._request(  # noqa: SLF001 — internal client helper
            "POST", "/api/v1/gtm/views/catalog/refresh",
            bearer_token=None, json={},
        )
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.get("")
async def list_endpoint(_auth: None = Depends(verify_backend_x_token)) -> dict[str, Any]:
    try:
        return await dex_client.list_gtm_views()
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: ViewSpecRequest,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.create_gtm_view(payload.model_dump())
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.get("/{view_id}")
async def get_endpoint(
    view_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.get_gtm_view(view_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.patch("/{view_id}")
async def patch_endpoint(
    view_id: UUID,
    payload: ViewPatchRequest,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    patch = payload.model_dump(exclude_none=True)
    try:
        return await dex_client.patch_gtm_view(view_id, patch)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.delete("/{view_id}")
async def delete_endpoint(
    view_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.delete_gtm_view(view_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("/{view_id}/compute")
async def compute_endpoint(
    view_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.compute_gtm_view(view_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc


@router.post("/{view_id}/materialize")
async def materialize_endpoint(
    view_id: UUID,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.materialize_gtm_view(view_id)
    except dex_client.DexCallError as exc:
        raise _proxy_dex_error(exc) from exc
