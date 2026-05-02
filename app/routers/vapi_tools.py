"""Vapi tools — passthrough CRUD.

Vapi tools live entirely on Vapi (single-operator account). The
``brand_id`` in the path is informational/auth-scoping only; tools are
not partitioned per-brand on the Vapi side. Future: if we move to a
multi-account-per-brand model, layer scoping in here.

Bodies accept ``dict[str, Any]`` (extra="allow") because Vapi's tool
schema is large and still evolving — see ``api-reference/tools/*.md``
for the current shape.
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
    prefix="/api/brands/{brand_id}/vapi/tools",
    tags=["vapi-tools"],
)


class VapiToolPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("", status_code=201)
async def create_tool(
    brand_id: UUID,
    body: VapiToolPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.create_tool(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("create_tool", exc)


@router.get("")
async def list_tools(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    api_key = vapi_key()
    try:
        return vapi_client.list_tools(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_tools", exc)


@router.get("/{tool_id}")
async def get_tool(
    brand_id: UUID,
    tool_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_tool(api_key, tool_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_tool", exc)


@router.patch("/{tool_id}")
async def update_tool(
    brand_id: UUID,
    tool_id: str,
    body: VapiToolPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    payload = body.model_dump()
    if not payload:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        return vapi_client.update_tool(api_key, tool_id, payload)
    except VapiProviderError as exc:
        raise_vapi_error("update_tool", exc)


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    brand_id: UUID,
    tool_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_tool(api_key, tool_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_tool", exc)
