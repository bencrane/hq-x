"""Vapi files — passthrough CRUD with multipart upload.

Wraps Vapi's POST /file (multipart/form-data). The upload is streamed
into memory with a 25 MiB cap (anything larger is rejected with 413
without forwarding to Vapi). Other endpoints are simple JSON
passthroughs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key

router = APIRouter(
    prefix="/api/brands/{brand_id}/vapi/files",
    tags=["vapi-files"],
)

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


class VapiFileUpdateRequest(BaseModel):
    name: str
    model_config = {"extra": "forbid"}


@router.post("", status_code=201)
async def upload_file(
    brand_id: UUID,
    file: UploadFile = File(...),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "file_too_large",
                "max_bytes": _MAX_UPLOAD_BYTES,
            },
        )
    api_key = vapi_key()
    filename = file.filename or "upload"
    content_type = file.content_type or "application/octet-stream"
    try:
        return vapi_client.create_file(api_key, contents, filename, content_type)
    except VapiProviderError as exc:
        raise_vapi_error("create_file", exc)


@router.get("")
async def list_files(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    api_key = vapi_key()
    try:
        return vapi_client.list_files(api_key)
    except VapiProviderError as exc:
        raise_vapi_error("list_files", exc)


@router.get("/{file_id}")
async def get_file(
    brand_id: UUID,
    file_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.get_file(api_key, file_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_file", exc)


@router.patch("/{file_id}")
async def update_file(
    brand_id: UUID,
    file_id: str,
    body: VapiFileUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.update_file(api_key, file_id, name=body.name)
    except VapiProviderError as exc:
        raise_vapi_error("update_file", exc)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    brand_id: UUID,
    file_id: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    api_key = vapi_key()
    try:
        vapi_client.delete_file(api_key, file_id)
    except VapiProviderError as exc:
        raise_vapi_error("delete_file", exc)
