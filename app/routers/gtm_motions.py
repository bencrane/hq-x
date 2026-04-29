"""REST surface for business.gtm_motions.

All routes are organization-scoped via ``require_org_context`` — the active
org is resolved by the X-Organization-Id header, and platform_operator users
can drive any org by setting the header explicitly. The motion's
organization_id always comes from the auth context, never the request body,
so a member of org A cannot create a motion in org B by tampering with the
payload.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.gtm import (
    GtmMotionCreate,
    GtmMotionResponse,
    GtmMotionUpdate,
    MotionStatus,
)
from app.services.gtm_motions import (
    MotionBrandMismatch,
    MotionNotFound,
    archive_motion,
    create_motion,
    get_motion,
    list_motions,
    update_motion,
)

router = APIRouter(prefix="/api/v1/gtm-motions", tags=["gtm-motions"])


@router.post("", response_model=GtmMotionResponse, status_code=status.HTTP_201_CREATED)
async def create_motion_route(
    payload: GtmMotionCreate,
    user: UserContext = Depends(require_org_context),
) -> GtmMotionResponse:
    assert user.active_organization_id is not None  # require_org_context guarantees
    try:
        return await create_motion(
            organization_id=user.active_organization_id,
            payload=payload,
            created_by_user_id=user.business_user_id,
        )
    except MotionBrandMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "brand_not_in_organization", "message": str(exc)},
        ) from exc


@router.get("", response_model=list[GtmMotionResponse])
async def list_motions_route(
    user: Annotated[UserContext, Depends(require_org_context)],
    brand_id: UUID | None = Query(default=None),
    motion_status: MotionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[GtmMotionResponse]:
    assert user.active_organization_id is not None
    return await list_motions(
        organization_id=user.active_organization_id,
        brand_id=brand_id,
        status=motion_status,
        limit=limit,
        offset=offset,
    )


@router.get("/{motion_id}", response_model=GtmMotionResponse)
async def get_motion_route(
    motion_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> GtmMotionResponse:
    assert user.active_organization_id is not None
    try:
        return await get_motion(
            motion_id=motion_id, organization_id=user.active_organization_id
        )
    except MotionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "motion_not_found"},
        ) from exc


@router.patch("/{motion_id}", response_model=GtmMotionResponse)
async def update_motion_route(
    motion_id: UUID,
    payload: GtmMotionUpdate,
    user: UserContext = Depends(require_org_context),
) -> GtmMotionResponse:
    assert user.active_organization_id is not None
    try:
        return await update_motion(
            motion_id=motion_id,
            organization_id=user.active_organization_id,
            payload=payload,
        )
    except MotionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "motion_not_found"},
        ) from exc


@router.post("/{motion_id}/archive", response_model=GtmMotionResponse)
async def archive_motion_route(
    motion_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> GtmMotionResponse:
    assert user.active_organization_id is not None
    try:
        return await archive_motion(
            motion_id=motion_id, organization_id=user.active_organization_id
        )
    except MotionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "motion_not_found"},
        ) from exc


__all__: list[Any] = ["router"]
