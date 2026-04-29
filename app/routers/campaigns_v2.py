"""REST surface for the channel-typed business.campaigns table.

This router replaces the brand-axis voice-only ``/api/brands/{brand_id}/voice/
campaigns`` surface (which still exists for back-compat against the
voice_ai_campaign_configs row) with an org-scoped, channel-agnostic API.

Every route resolves the campaign by (id, organization_id) so members of one
org cannot read or mutate another org's campaigns even if they guess the
campaign UUID. Platform operators drive other orgs by setting
X-Organization-Id, the same pattern used by gtm-motions.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.gtm import (
    CampaignCreate,
    CampaignResponse,
    CampaignStatus,
    CampaignUpdate,
    Channel,
)
from app.services.campaigns import (
    CampaignChannelProviderInvalid,
    CampaignDesignBrandMismatch,
    CampaignDesignRequired,
    CampaignInvalidStatusTransition,
    CampaignNotFound,
    activate_campaign,
    archive_campaign,
    create_campaign,
    get_campaign,
    list_campaigns,
    pause_campaign,
    resume_campaign,
    update_campaign,
)
from app.services.gtm_motions import MotionNotFound

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


def _bad_request(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _not_found(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


def _conflict(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": error, "message": message},
    )


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign_route(
    payload: CampaignCreate,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await create_campaign(
            organization_id=user.active_organization_id,
            payload=payload,
            created_by_user_id=user.business_user_id,
        )
    except MotionNotFound as exc:
        raise _not_found("motion_not_found") from exc
    except CampaignChannelProviderInvalid as exc:
        raise _bad_request("invalid_channel_provider", str(exc)) from exc
    except CampaignDesignRequired as exc:
        raise _bad_request("design_required", str(exc)) from exc
    except CampaignDesignBrandMismatch as exc:
        raise _bad_request("design_brand_mismatch", str(exc)) from exc


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns_route(
    user: Annotated[UserContext, Depends(require_org_context)],
    motion_id: UUID | None = Query(default=None),
    channel: Channel | None = Query(default=None),
    campaign_status: CampaignStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CampaignResponse]:
    assert user.active_organization_id is not None
    return await list_campaigns(
        organization_id=user.active_organization_id,
        motion_id=motion_id,
        channel=channel,
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
        raise _not_found("campaign_not_found") from exc


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
        raise _not_found("campaign_not_found") from exc
    except CampaignDesignBrandMismatch as exc:
        raise _bad_request("design_brand_mismatch", str(exc)) from exc


@router.post("/{campaign_id}/activate", response_model=CampaignResponse)
async def activate_campaign_route(
    campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await activate_campaign(
            campaign_id=campaign_id, organization_id=user.active_organization_id
        )
    except CampaignNotFound as exc:
        raise _not_found("campaign_not_found") from exc
    except CampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


@router.post("/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign_route(
    campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await pause_campaign(
            campaign_id=campaign_id, organization_id=user.active_organization_id
        )
    except CampaignNotFound as exc:
        raise _not_found("campaign_not_found") from exc
    except CampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


@router.post("/{campaign_id}/resume", response_model=CampaignResponse)
async def resume_campaign_route(
    campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> CampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await resume_campaign(
            campaign_id=campaign_id, organization_id=user.active_organization_id
        )
    except CampaignNotFound as exc:
        raise _not_found("campaign_not_found") from exc
    except CampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


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
        raise _not_found("campaign_not_found") from exc
    except CampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


__all__: list[Any] = ["router"]
