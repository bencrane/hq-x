"""Admin CRUD for outreach_model_emails — operator-only.

The per-recipient creative bundle pulls these as voice/style anchors.
Surface stays narrow: operator manages a small library (typically 3-12
rows per audience type) by hand and via the seed-script.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.services import outreach_model_emails as ome_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/outreach-model-emails",
    tags=["admin", "outreach-model-emails"],
)


class CreateModelEmailRequest(BaseModel):
    organization_id: UUID
    brand_id: UUID | None = None
    purpose: str
    audience_template_slug: str | None = None
    step_index: int | None = Field(default=None, gt=0)
    label: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class UpdateModelEmailRequest(BaseModel):
    purpose: str | None = None
    audience_template_slug: str | None = None
    step_index: int | None = Field(default=None, gt=0)
    label: str | None = None
    subject: str | None = None
    body: str | None = None
    notes: str | None = None
    is_active: bool | None = None
    brand_id: UUID | None = None
    model_config = {"extra": "forbid"}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_model_email(
    body: CreateModelEmailRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await ome_svc.create(
            organization_id=body.organization_id,
            brand_id=body.brand_id,
            purpose=body.purpose,
            audience_template_slug=body.audience_template_slug,
            step_index=body.step_index,
            label=body.label,
            subject=body.subject,
            body=body.body,
            notes=body.notes,
            metadata=body.metadata,
            created_by_user_id=user.business_user_id,
        )
    except ome_svc.OutreachModelEmailValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc


@router.get("")
async def list_model_emails(
    organization_id: UUID,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    items = await ome_svc.list_all(
        organization_id=organization_id,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{model_email_id}")
async def read_model_email(
    model_email_id: UUID,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await ome_svc.get(model_email_id)
    except ome_svc.OutreachModelEmailValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc


@router.patch("/{model_email_id}")
async def update_model_email(
    model_email_id: UUID,
    body: UpdateModelEmailRequest,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True, exclude_none=False)
    try:
        return await ome_svc.update(model_email_id=model_email_id, fields=fields)
    except ome_svc.OutreachModelEmailValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc


@router.delete("/{model_email_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_email(
    model_email_id: UUID,
    _: UserContext = Depends(require_platform_operator),
) -> None:
    await ome_svc.delete(model_email_id)


__all__ = ["router"]
