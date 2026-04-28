"""Voice provisioning entry-point.

Brand-axis port of OEX ``routers/provisioning.py``. The OEX router managed a
persistent ``company_provisioning_runs`` ledger; in hq-x the ledger is
deferred (no ``companies`` table, single-operator world). This router exposes
a single synchronous endpoint that drives the 8-step pipeline and returns
the structured result.

The persistent state for a successful run lives in the side-effect tables:
``trust_hub_registrations``, ``voice_phone_numbers``, ``ivr_phone_configs``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.twilio._http import TwilioProviderError
from app.services import brands as brands_svc
from app.services.pipelines.voice import execute_voice_pipeline

router = APIRouter(
    prefix="/api/brands/{brand_id}/provisioning",
    tags=["provisioning"],
)


class BusinessInfo(BaseModel):
    business_name: str
    business_type: str | None = None
    business_industry: str | None = None
    business_registration_id_type: str | None = None
    business_registration_id: str | None = None
    business_regions_of_operation: list[str] | None = None
    website: str | None = None
    model_config = {"extra": "allow"}


class AuthorizedRepresentative(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone_number: str
    job_position: str | None = None
    business_title: str | None = None
    model_config = {"extra": "allow"}


class Address(BaseModel):
    street: str
    city: str
    region: str
    postal_code: str
    iso_country: str = "US"
    customer_name: str | None = None
    model_config = {"extra": "allow"}


class PhoneNumberSearch(BaseModel):
    area_code: str | None = None
    country_code: str = "US"


class PhoneConfig(BaseModel):
    count: int = Field(ge=0, le=20)
    area_code: str | None = None
    country_code: str = "US"


class ProvisionVoiceRequest(BaseModel):
    phone_config: PhoneConfig | None = None
    business_info: BusinessInfo | None = None
    authorized_representative: AuthorizedRepresentative | None = None
    authorized_representative_2: AuthorizedRepresentative | None = None
    address: Address | None = None
    notification_email: str | None = None
    ivr_template_flow_id: UUID | None = None
    sms_enabled: bool = False
    model_config = {"extra": "forbid"}


@router.post("/voice", status_code=status.HTTP_200_OK)
async def provision_voice(
    brand_id: UUID,
    body: ProvisionVoiceRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Run the voice provisioning pipeline for a brand.

    Returns 200 OK with the structured result. Steps may individually fail —
    inspect ``result["steps"][step_name]["status"]``. The aggregate
    ``result["status"]`` is one of ``completed`` / ``partial`` / ``unknown``.
    """
    # Validate creds presence early so we 400 before any side effects.
    try:
        creds = await brands_svc.get_twilio_creds(brand_id)
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "Brand has no Twilio credentials configured"},
        )

    if body.business_info is not None:
        if body.authorized_representative is None:
            raise HTTPException(
                status_code=400,
                detail="authorized_representative is required when business_info is provided",
            )
        if body.address is None:
            raise HTTPException(
                status_code=400,
                detail="address is required when business_info is provided",
            )
        if body.notification_email is None:
            raise HTTPException(
                status_code=400,
                detail="notification_email is required when business_info is provided",
            )

    config: dict[str, Any] = {}
    if body.business_info:
        config["business_info"] = body.business_info.model_dump()
    if body.authorized_representative:
        config["authorized_representative"] = body.authorized_representative.model_dump()
    if body.authorized_representative_2:
        config["authorized_representative_2"] = body.authorized_representative_2.model_dump()
    if body.address:
        config["address"] = body.address.model_dump()
    if body.notification_email:
        config["notification_email"] = body.notification_email
    if body.phone_config:
        config["phone_numbers_to_purchase"] = body.phone_config.count
        config["phone_number_search"] = {
            "area_code": body.phone_config.area_code,
            "country_code": body.phone_config.country_code,
        }
    if body.ivr_template_flow_id:
        config["ivr_template_flow_id"] = body.ivr_template_flow_id
    config["sms_enabled"] = body.sms_enabled

    try:
        result = await execute_voice_pipeline(brand_id=brand_id, config=config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TwilioProviderError as exc:
        http_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=http_status,
            detail={
                "type": "provider_error",
                "provider": "twilio",
                "retryable": exc.retryable,
                "message": str(exc),
            },
        ) from exc
    return result
