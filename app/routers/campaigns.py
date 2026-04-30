"""REST surface for business.campaigns (the umbrella outreach effort).

All routes are organization-scoped via ``require_org_context`` — the active
org is resolved by the X-Organization-Id header, and platform_operator users
can drive any org by setting the header explicitly. The campaign's
organization_id always comes from the auth context, never the request body,
so a member of org A cannot create a campaign in org B by tampering with
the payload.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.campaigns import (
    CampaignCreate,
    CampaignResponse,
    CampaignStatus,
    CampaignUpdate,
)
from app.services.campaigns import (
    CampaignBrandMismatch,
    CampaignNotFound,
    archive_campaign,
    create_campaign,
    get_campaign,
    list_campaigns,
    update_campaign,
)

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign_route(
    payload: CampaignCreate,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None  # require_org_context guarantees
    try:
        return await create_campaign(
            organization_id=user.active_organization_id,
            payload=payload,
            created_by_user_id=user.business_user_id,
        )
    except CampaignBrandMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "brand_not_in_organization", "message": str(exc)},
        ) from exc


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns_route(
    user: Annotated[UserContext, Depends(require_org_context)],
    brand_id: UUID | None = Query(default=None),
    campaign_status: CampaignStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CampaignResponse]:
    assert user.active_organization_id is not None
    return await list_campaigns(
        organization_id=user.active_organization_id,
        brand_id=brand_id,
        status=campaign_status,
        limit=limit,
        offset=offset,
    )


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign_route(
    campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await get_campaign(
            campaign_id=campaign_id, organization_id=user.active_organization_id
        )
    except CampaignNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "campaign_not_found"},
        ) from exc


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign_route(
    campaign_id: UUID,
    payload: CampaignUpdate,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await update_campaign(
            campaign_id=campaign_id,
            organization_id=user.active_organization_id,
            payload=payload,
        )
    except CampaignNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "campaign_not_found"},
        ) from exc


@router.post("/{campaign_id}/archive", response_model=CampaignResponse)
async def archive_campaign_route(
    campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await archive_campaign(
            campaign_id=campaign_id, organization_id=user.active_organization_id
        )
    except CampaignNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "campaign_not_found"},
        ) from exc


__all__: list[Any] = ["router"]
