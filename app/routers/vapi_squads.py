"""Vapi squads — passthrough CRUD.

Squads (multi-assistant flows) live entirely on Vapi. Brand scoping is
informational only; no local mirror table is created in V1.
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
    prefix="/api/brands/{brand_id}/vapi/squads",
    tags=["vapi-squads"],
)


class VapiSquadPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("", status_code=201)
async def create_squad(
    brand_id: UUID,
    body: VapiSquadPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.create_squad(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("create_squad", exc)


@router.get("")
async def list_squads(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    api_key = vapi_key()
    try:
        return vapi_client.list_squads(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_squads", exc)


@router.get("/{squad_id}")
async def get_squad(
    brand_id: UUID,
    squad_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_squad(api_key, squad_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_squad", exc)


@router.patch("/{squad_id}")
async def update_squad(
    brand_id: UUID,
    squad_id: str,
    body: VapiSquadPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    payload = body.model_dump()
    if not payload:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        return vapi_client.update_squad(api_key, squad_id, payload)
    except VapiProviderError as exc:
        raise_vapi_error("update_squad", exc)


@router.delete("/{squad_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_squad(
    brand_id: UUID,
    squad_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_squad(api_key, squad_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_squad", exc)
