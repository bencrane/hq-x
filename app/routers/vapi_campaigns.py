"""Vapi campaigns — passthrough CRUD against Vapi's /campaign resource.

Distinct from ``app/routers/voice_campaigns.py``, which manages the
local ``voice_ai_campaign_configs`` table for our own scheduling logic.
This router targets Vapi's built-in dashboard-style campaigns
(``POST /campaign``); pick this surface only when you want Vapi to own
the orchestration end-to-end.
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
    prefix="/api/brands/{brand_id}/vapi/campaigns",
    tags=["vapi-campaigns"],
)


class VapiCampaignPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("", status_code=201)
async def create_campaign(
    brand_id: UUID,
    body: VapiCampaignPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.create_campaign(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("create_campaign", exc)


@router.get("")
async def list_campaigns(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.list_campaigns(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_campaigns", exc)


@router.get("/{campaign_id}")
async def get_campaign(
    brand_id: UUID,
    campaign_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_campaign(api_key, campaign_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_campaign", exc)


@router.patch("/{campaign_id}")
async def update_campaign(
    brand_id: UUID,
    campaign_id: str,
    body: VapiCampaignPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    payload = body.model_dump()
    if not payload:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        return vapi_client.update_campaign(api_key, campaign_id, payload)
    except VapiProviderError as exc:
        raise_vapi_error("update_campaign", exc)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    brand_id: UUID,
    campaign_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_campaign(api_key, campaign_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_campaign", exc)
