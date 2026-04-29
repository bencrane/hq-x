"""Vapi insights — passthrough CRUD + preview/run.

Targets Vapi's ``/reporting/insight`` resource. Insights are saved
queries (bar/line/pie/text visualizations) over the call + events
tables; ``preview`` runs an unsaved insight, and ``{id}/run`` executes a
saved insight (with optional time-range override).

The ``brand_id`` in the path is informational/auth-scoping only — Vapi's
insights are account-wide on the single-operator Vapi account, not
per-brand. Future: if we move to a multi-account-per-brand model, layer
scoping in here.

Bodies accept ``dict[str, Any]`` (extra="allow") because Vapi's insight
schema is a large discriminated union (bar/pie/line/text) — see
``api-reference/insight/*.md`` for the current shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key

router = APIRouter(
    prefix="/api/brands/{brand_id}/vapi/insights",
    tags=["vapi-insights"],
)


class VapiInsightPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("", status_code=201)
async def create_insight(
    brand_id: UUID,
    body: VapiInsightPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.create_insight(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("create_insight", exc)


@router.get("")
async def list_insights(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.list_insights(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_insights", exc)


@router.post("/preview")
async def preview_insight(
    brand_id: UUID,
    body: VapiInsightPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.preview_insight(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("preview_insight", exc)


@router.get("/{insight_id}")
async def get_insight(
    brand_id: UUID,
    insight_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_insight(api_key, insight_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_insight", exc)


@router.patch("/{insight_id}")
async def update_insight(
    brand_id: UUID,
    insight_id: str,
    body: VapiInsightPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    payload = body.model_dump()
    if not payload:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        return vapi_client.update_insight(api_key, insight_id, payload)
    except VapiProviderError as exc:
        raise_vapi_error("update_insight", exc)


@router.delete("/{insight_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_insight(
    brand_id: UUID,
    insight_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_insight(api_key, insight_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_insight", exc)


@router.post("/{insight_id}/run")
async def run_insight(
    brand_id: UUID,
    insight_id: str,
    body: VapiInsightPayload | None = None,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    config = body.model_dump() if body is not None else None
    try:
        return vapi_client.run_insight(api_key, insight_id, config)
    except VapiProviderError as exc:
        raise_vapi_error("run_insight", exc)
