"""REST surface for business.channel_campaign_steps.

Two URL families:

  * Nested under a parent channel_campaign for create + list:
      POST /api/v1/channel-campaigns/{cc_id}/steps
      GET  /api/v1/channel-campaigns/{cc_id}/steps
  * Flat by step id for read/update/lifecycle:
      GET    /api/v1/channel-campaign-steps/{step_id}
      PATCH  /api/v1/channel-campaign-steps/{step_id}
      POST   /api/v1/channel-campaign-steps/{step_id}/activate
      POST   /api/v1/channel-campaign-steps/{step_id}/cancel

All routes are organization-scoped via ``require_org_context``. The parent
channel_campaign must belong to the active organization for create + list;
the service layer enforces this via the (id, organization_id) pairing on
``get_channel_campaign``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.campaigns import (
    ChannelCampaignStepCreate,
    ChannelCampaignStepResponse,
    ChannelCampaignStepStatus,
    ChannelCampaignStepUpdate,
)
from app.services.channel_campaign_steps import (
    StepActivationNotImplemented,
    StepCreativeRefBrandMismatch,
    StepCreativeRefRequired,
    StepImmutable,
    StepInvalidStatusTransition,
    StepNotFound,
    activate_step,
    cancel_step,
    create_step,
    get_step,
    list_steps,
    update_step,
)
from app.services.channel_campaigns import ChannelCampaignNotFound

# Two router instances so the URL prefixes can differ; both mounted in main.
nested_router = APIRouter(
    prefix="/api/v1/channel-campaigns",
    tags=["channel-campaign-steps"],
)
flat_router = APIRouter(
    prefix="/api/v1/channel-campaign-steps",
    tags=["channel-campaign-steps"],
)


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


# ── Nested under {channel_campaign_id} ────────────────────────────────────


@nested_router.post(
    "/{channel_campaign_id}/steps",
    response_model=ChannelCampaignStepResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_step_route(
    channel_campaign_id: UUID,
    payload: ChannelCampaignStepCreate,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignStepResponse:
    assert user.active_organization_id is not None
    try:
        return await create_step(
            channel_campaign_id=channel_campaign_id,
            organization_id=user.active_organization_id,
            payload=payload,
        )
    except ChannelCampaignNotFound as exc:
        raise _not_found("channel_campaign_not_found") from exc
    except StepCreativeRefRequired as exc:
        raise _bad_request("creative_ref_required", str(exc)) from exc
    except StepCreativeRefBrandMismatch as exc:
        raise _bad_request("creative_ref_brand_mismatch", str(exc)) from exc


@nested_router.get(
    "/{channel_campaign_id}/steps",
    response_model=list[ChannelCampaignStepResponse],
)
async def list_steps_route(
    channel_campaign_id: UUID,
    user: Annotated[UserContext, Depends(require_org_context)],
    step_status: ChannelCampaignStepStatus | None = Query(
        default=None, alias="status"
    ),
) -> list[ChannelCampaignStepResponse]:
    assert user.active_organization_id is not None
    return await list_steps(
        channel_campaign_id=channel_campaign_id,
        organization_id=user.active_organization_id,
        status=step_status,
    )


# ── Flat /api/v1/channel-campaign-steps/{step_id} ─────────────────────────


@flat_router.get(
    "/{step_id}", response_model=ChannelCampaignStepResponse
)
async def get_step_route(
    step_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignStepResponse:
    assert user.active_organization_id is not None
    try:
        return await get_step(
            step_id=step_id, organization_id=user.active_organization_id
        )
    except StepNotFound as exc:
        raise _not_found("channel_campaign_step_not_found") from exc


@flat_router.patch(
    "/{step_id}", response_model=ChannelCampaignStepResponse
)
async def update_step_route(
    step_id: UUID,
    payload: ChannelCampaignStepUpdate,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignStepResponse:
    assert user.active_organization_id is not None
    try:
        return await update_step(
            step_id=step_id,
            organization_id=user.active_organization_id,
            payload=payload,
        )
    except StepNotFound as exc:
        raise _not_found("channel_campaign_step_not_found") from exc
    except StepImmutable as exc:
        raise _conflict("step_immutable", str(exc)) from exc
    except StepCreativeRefBrandMismatch as exc:
        raise _bad_request("creative_ref_brand_mismatch", str(exc)) from exc


@flat_router.post(
    "/{step_id}/activate", response_model=ChannelCampaignStepResponse
)
async def activate_step_route(
    step_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignStepResponse:
    assert user.active_organization_id is not None
    try:
        return await activate_step(
            step_id=step_id, organization_id=user.active_organization_id
        )
    except StepNotFound as exc:
        raise _not_found("channel_campaign_step_not_found") from exc
    except StepInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc
    except StepActivationNotImplemented as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error": "activation_not_implemented", "message": str(exc)},
        ) from exc


@flat_router.post(
    "/{step_id}/cancel", response_model=ChannelCampaignStepResponse
)
async def cancel_step_route(
    step_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> ChannelCampaignStepResponse:
    assert user.active_organization_id is not None
    try:
        return await cancel_step(
            step_id=step_id, organization_id=user.active_organization_id
        )
    except StepNotFound as exc:
        raise _not_found("channel_campaign_step_not_found") from exc
    except StepInvalidStatusTransition as exc:
        raise _conflict("invalid_status_transition", str(exc)) from exc


__all__: list[Any] = ["nested_router", "flat_router"]
