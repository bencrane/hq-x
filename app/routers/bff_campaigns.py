"""hq-zone BFF-facing campaign-enrollment route.

Single atomic endpoint that lets the platform-api BFF take a lead list and
land a full campaign + channel_campaign + first step + recipients +
step memberships in one round trip.

Auth: ``verify_backend_x_token`` (shared service token shared with the BFF
via Doppler hq-zone/prd + hq-all/prd). No per-user Supabase JWT — the BFF
asserts its own identity and supplies organization_id + brand_id in the
request body. Mirrors the auth pattern used by ``app/routers/gtm_people.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.service_token import verify_backend_x_token
from app.models.bff_campaigns import BffEnrollListRequest, BffEnrollListResponse
from app.services.bff_campaigns import (
    BffEnrollBrandMismatch,
    BffEnrollInvalidChannelProvider,
    enroll_list_into_new_campaign,
)

router = APIRouter(prefix="/api/v1/bff/campaigns", tags=["bff-campaigns"])


@router.post(
    "/enroll-list",
    response_model=BffEnrollListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_list_route(
    payload: BffEnrollListRequest,
    _auth: None = Depends(verify_backend_x_token),
) -> BffEnrollListResponse:
    try:
        return await enroll_list_into_new_campaign(payload)
    except BffEnrollBrandMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "brand_not_in_organization", "message": str(exc)},
        ) from exc
    except BffEnrollInvalidChannelProvider as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_channel_provider", "message": str(exc)},
        ) from exc


__all__ = ["router"]
