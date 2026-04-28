"""Brand CRUD — operator-only.

Brands are the compliance/marketing identity in single-operator world.
This router lets the operator create, read, list, and update brands;
encrypted Twilio credentials flow through services/brands.py.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.services import brands as brands_svc

router = APIRouter(prefix="/admin/brands", tags=["brands"])


class BrandCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = None
    domain: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_messaging_service_sid: str | None = None

    model_config = {"extra": "forbid"}


class BrandTwilioCredsUpdateRequest(BaseModel):
    twilio_account_sid: str = Field(min_length=1)
    twilio_auth_token: str = Field(min_length=1)

    model_config = {"extra": "forbid"}


class BrandResponse(BaseModel):
    id: UUID
    name: str
    display_name: str | None
    domain: str | None
    twilio_messaging_service_sid: str | None
    primary_customer_profile_sid: str | None
    trust_hub_registration_id: UUID | None
    has_twilio_creds: bool


def _to_response(b: brands_svc.Brand, *, has_creds: bool) -> BrandResponse:
    return BrandResponse(
        id=b.id,
        name=b.name,
        display_name=b.display_name,
        domain=b.domain,
        twilio_messaging_service_sid=b.twilio_messaging_service_sid,
        primary_customer_profile_sid=b.primary_customer_profile_sid,
        trust_hub_registration_id=b.trust_hub_registration_id,
        has_twilio_creds=has_creds,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_brand_endpoint(
    body: BrandCreateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> BrandResponse:
    try:
        brand_id = await brands_svc.create_brand(
            name=body.name,
            display_name=body.display_name,
            domain=body.domain,
            twilio_account_sid=body.twilio_account_sid,
            twilio_auth_token=body.twilio_auth_token,
            twilio_messaging_service_sid=body.twilio_messaging_service_sid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)})

    brand = await brands_svc.get_brand(brand_id)
    assert brand is not None
    return _to_response(brand, has_creds=body.twilio_account_sid is not None)


@router.get("")
async def list_brands_endpoint(
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[BrandResponse]:
    brands = await brands_svc.list_brands()
    out: list[BrandResponse] = []
    for b in brands:
        creds = None
        try:
            creds = await brands_svc.get_twilio_creds(b.id)
        except brands_svc.BrandCredsKeyMissing:
            pass
        out.append(_to_response(b, has_creds=creds is not None))
    return out


@router.get("/{brand_id}")
async def get_brand_endpoint(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> BrandResponse:
    brand = await brands_svc.get_brand(brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found"})
    has_creds = False
    try:
        has_creds = (await brands_svc.get_twilio_creds(brand_id)) is not None
    except brands_svc.BrandCredsKeyMissing:
        pass
    return _to_response(brand, has_creds=has_creds)


@router.put("/{brand_id}/twilio-creds", status_code=status.HTTP_204_NO_CONTENT)
async def update_twilio_creds_endpoint(
    brand_id: UUID,
    body: BrandTwilioCredsUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    brand = await brands_svc.get_brand(brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found"})
    try:
        await brands_svc.set_twilio_creds(
            brand_id,
            account_sid=body.twilio_account_sid,
            auth_token=body.twilio_auth_token,
        )
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)})
