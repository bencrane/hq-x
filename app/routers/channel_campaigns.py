"""REST surface for business.channel_campaigns.

A channel_campaign is the per-channel execution unit underneath a campaign
(channel ∈ {direct_mail, email, voice_outbound, sms}). Every route resolves
the row by (id, organization_id) so members of one org cannot read or
mutate another org's channel campaigns even if they guess the UUID.
Platform operators drive other orgs by setting X-Organization-Id, the same
pattern used by the umbrella ``campaigns`` router.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.campaigns import (
    Channel,
    ChannelCampaignCreate,
    ChannelCampaignResponse,
    ChannelCampaignStatus,
    ChannelCampaignUpdate,
)
from app.services.campaigns import CampaignNotFound
from app.services.channel_campaigns import (
    ChannelCampaignChannelProviderInvalid,
    ChannelCampaignDesignBrandMismatch,
    ChannelCampaignDesignRequired,
    ChannelCampaignInvalidStatusTransition,
    ChannelCampaignNotFound,
    activate_channel_campaign,
    archive_channel_campaign,
    create_channel_campaign,
    get_channel_campaign,
    list_channel_campaigns,
    pause_channel_campaign,
    resume_channel_campaign,
    update_channel_campaign,
)

router = APIRouter(prefix="/api/v1/channel-campaigns", tags=["channel-campaigns"])


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


@router.post(
    "", response_model=ChannelCampaignResponse, status_code=status.HTTP_201_CREATED
)
async def create_channel_campaign_route(
    payload: ChannelCampaignCreate,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await create_channel_campaign(
            organization_id=user.active_organization_id,
            payload=payload,
            created_by_user_id=user.business_user_id,
        )
    except CampaignNotFound as exc:
        raise _not_found("campaign_not_found") from exc
    except ChannelCampaignChannelProviderInvalid as exc:
        raise _bad_request("invalid_channel_provider", str(exc)) from exc
    except ChannelCampaignDesignRequired as exc:
        raise _bad_request("design_required", str(exc)) from exc
    except ChannelCampaignDesignBrandMismatch as exc:
        raise _bad_request("design_brand_mismatch", str(exc)) from exc


@router.get("", response_model=list[ChannelCampaignResponse])
async def list_channel_campaigns_route(
    user: Annotated[UserContext, Depends(require_org_context)],
    campaign_id: UUID | None = Query(default=None),
    channel: Channel | None = Query(default=None),
    cc_status: ChannelCampaignStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ChannelCampaignResponse]:
    assert user.active_organization_id is not None
    return await list_channel_campaigns(
        organization_id=user.active_organization_id,
        campaign_id=campaign_id,
        channel=channel,
        status=cc_status,
        limit=limit,
        offset=offset,
    )


@router.get("/{channel_campaign_id}", response_model=ChannelCampaignResponse)
async def get_channel_campaign_route(
    channel_campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await get_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc


@router.patch("/{channel_campaign_id}", response_model=ChannelCampaignResponse)
async def update_channel_campaign_route(
    channel_campaign_id: UUID,
    payload: ChannelCampaignUpdate,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await update_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
            payload=payload,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except ChannelCampaignDesignBrandMismatch as exc:
        raise _bad_request("design_brand_mismatch", str(exc)) from exc


@router.post("/{channel_campaign_id}/activate", response_model=ChannelCampaignResponse)
async def activate_channel_campaign_route(
    channel_campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await activate_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except ChannelCampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


@router.post("/{channel_campaign_id}/pause", response_model=ChannelCampaignResponse)
async def pause_channel_campaign_route(
    channel_campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await pause_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except ChannelCampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


@router.post("/{channel_campaign_id}/resume", response_model=ChannelCampaignResponse)
async def resume_channel_campaign_route(
    channel_campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await resume_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except ChannelCampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


@router.post("/{channel_campaign_id}/archive", response_model=ChannelCampaignResponse)
async def archive_channel_campaign_route(
    channel_campaign_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignResponse:
    assert user.active_organization_id is not None
    try:
        return await archive_channel_campaign(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except ChannelCampaignInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


__all__: list[Any] = ["router"]
