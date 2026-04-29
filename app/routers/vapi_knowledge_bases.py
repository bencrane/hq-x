"""Vapi knowledge bases — passthrough CRUD.

Targets Vapi's ``/knowledge-base`` resource (path documented in
``vapi/knowledge-base/custom-knowledge-base.md`` — note that Vapi's
public API reference has not yet promoted KBs to a top-level CRUD
listing, so the live docs site does not surface this CRUD; the canonical
local doc + the cURL example in ``custom-knowledge-base.md`` are the
authoritative source until Vapi catches up).
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
    prefix="/api/brands/{brand_id}/vapi/knowledge-bases",
    tags=["vapi-knowledge-bases"],
)


class VapiKnowledgeBasePayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("", status_code=201)
async def create_knowledge_base(
    brand_id: UUID,
    body: VapiKnowledgeBasePayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.create_knowledge_base(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("create_knowledge_base", exc)


@router.get("")
async def list_knowledge_bases(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    api_key = vapi_key()
    try:
        return vapi_client.list_knowledge_bases(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_knowledge_bases", exc)


@router.get("/{kb_id}")
async def get_knowledge_base(
    brand_id: UUID,
    kb_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_knowledge_base(api_key, kb_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_knowledge_base", exc)


@router.patch("/{kb_id}")
async def update_knowledge_base(
    brand_id: UUID,
    kb_id: str,
    body: VapiKnowledgeBasePayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    payload = body.model_dump()
    if not payload:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    try:
        return vapi_client.update_knowledge_base(api_key, kb_id, payload)
    except VapiProviderError as exc:
        raise_vapi_error("update_knowledge_base", exc)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    brand_id: UUID,
    kb_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_knowledge_base(api_key, kb_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_knowledge_base", exc)
