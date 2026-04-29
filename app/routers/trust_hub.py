"""Trust Hub management API + Twilio status callback receiver.

Drift fix §7.1: the Twilio status callback now validates the
X-Twilio-Signature header against the brand's auth_token. OEX shipped this
endpoint unsigned.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.webhooks import (
    reconstruct_public_url,
    validate_twilio_signature,
)
from app.services import brands as brands_svc
from app.services import trust_hub as trust_hub_service

router = APIRouter(prefix="/api/trust-hub", tags=["trust-hub"])
webhook_router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# Request / response models (kept inline; small surface)
# ---------------------------------------------------------------------------


class BusinessInfo(BaseModel):
    business_name: str
    business_type: str | None = None
    business_industry: str | None = None
    business_registration_identifier: str | None = None
    business_registration_number: str | None = None
    website_url: str | None = None
    model_config = {"extra": "allow"}


class Representative(BaseModel):
    first_name: str
    last_name: str
    email: str | None = None
    phone_number: str | None = None
    job_position: str | None = None
    business_title: str | None = None
    model_config = {"extra": "allow"}


class Address(BaseModel):
    customer_name: str
    street: str
    city: str
    region: str
    postal_code: str
    iso_country: str
    street_secondary: str | None = None
    model_config = {"extra": "forbid"}


class RegisterBrandRequest(BaseModel):
    notification_email: str
    primary_customer_profile_sid: str | None = Field(
        default=None,
        description=(
            "Primary CustomerProfile SID. Required if not already set on the brand row. "
            "Falls back to brands.primary_customer_profile_sid otherwise."
        ),
    )
    registration_types: list[str] = Field(default_factory=lambda: ["customer_profile"])
    business_info: BusinessInfo
    authorized_representative: Representative
    authorized_representative_2: Representative | None = None
    address: Address
    policy_sids: dict[str, str] | None = None
    model_config = {"extra": "forbid"}


class RegistrationSummary(BaseModel):
    registration_id: str
    registration_type: str
    status: str
    bundle_sid: str | None
    evaluation_status: str | None


class RegisterBrandResponse(BaseModel):
    registrations: list[RegistrationSummary]


class AssignPhoneNumberRequest(BaseModel):
    phone_number_sid: str
    bundle_types: list[str] = Field(default_factory=lambda: ["customer_profile"])
    model_config = {"extra": "forbid"}


class PhoneNumberAssignment(BaseModel):
    bundle_type: str
    bundle_sid: str | None = None
    status: str
    assignment_sid: str | None = None
    error: str | None = None
    reason: str | None = None


class PhoneNumberAssignmentResponse(BaseModel):
    phone_number_sid: str
    assignments: list[PhoneNumberAssignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_twilio_creds(brand_id: UUID) -> brands_svc.BrandTwilioCreds:
    try:
        creds = await brands_svc.get_twilio_creds(brand_id)
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "Brand has no Twilio credentials configured"},
        )
    return creds


def _raise_provider_error(operation: str, exc: TwilioProviderError) -> None:
    code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.retryable
        else status.HTTP_502_BAD_GATEWAY
    )
    raise HTTPException(
        status_code=code,
        detail={
            "type": "provider_error",
            "provider": "twilio",
            "operation": operation,
            "retryable": exc.retryable,
            "message": str(exc),
        },
    ) from exc


# ---------------------------------------------------------------------------
# Management API endpoints
# ---------------------------------------------------------------------------


@router.post("/brands/{brand_id}/register", response_model=RegisterBrandResponse)
async def register_brand(
    brand_id: UUID,
    body: RegisterBrandRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> RegisterBrandResponse:
    """Run the full Trust Hub registration workflow for a brand."""
    brand = await brands_svc.get_brand(brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found"})

    creds = await _resolve_twilio_creds(brand_id)

    primary_sid = body.primary_customer_profile_sid or brand.primary_customer_profile_sid
    if not primary_sid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "primary_customer_profile_sid is required (none set on brand row)",
            },
        )

    reg_types = list(body.registration_types)
    if "customer_profile" not in reg_types:
        reg_types.insert(0, "customer_profile")

    results: list[dict[str, Any]] = []

    try:
        profile_result = await trust_hub_service.register_customer_profile(
            brand_id=brand_id,
            account_sid=creds.account_sid,
            auth_token=creds.auth_token,
            primary_customer_profile_sid=primary_sid,
            notification_email=body.notification_email,
            business_info=body.business_info.model_dump(),
            representative=body.authorized_representative.model_dump(),
            representative_2=(
                body.authorized_representative_2.model_dump()
                if body.authorized_representative_2
                else None
            ),
            address=body.address.model_dump(),
            policy_sids=body.policy_sids,
        )
        results.append(profile_result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except TwilioProviderError as exc:
        _raise_provider_error("register_customer_profile", exc)

    if profile_result["status"] in ("pending-review", "twilio-approved"):
        for reg_type in reg_types:
            if reg_type == "customer_profile":
                continue
            try:
                product_result = await trust_hub_service.create_trust_product_registration(
                    brand_id=brand_id,
                    account_sid=creds.account_sid,
                    auth_token=creds.auth_token,
                    notification_email=body.notification_email,
                    registration_type=reg_type,
                    customer_profile_sid=profile_result["bundle_sid"],
                    policy_sids=body.policy_sids,
                )
                results.append(product_result)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
            except TwilioProviderError as exc:
                _raise_provider_error(f"register_{reg_type}", exc)

    return RegisterBrandResponse(
        registrations=[
            RegistrationSummary(
                registration_id=r["registration_id"],
                registration_type=r["registration_type"],
                status=r["status"],
                bundle_sid=r.get("bundle_sid"),
                evaluation_status=r.get("evaluation_status"),
            )
            for r in results
        ]
    )


@router.get("/brands/{brand_id}/registrations")
async def list_registrations(
    brand_id: UUID,
    registration_type: str | None = Query(default=None),
    registration_status: str | None = Query(default=None, alias="status"),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    from app.db import get_db_connection

    sql = """
        SELECT id, brand_id, registration_type, status, bundle_sid,
               policy_sid, evaluation_status, submitted_at, approved_at,
               rejected_at, created_at, updated_at
        FROM trust_hub_registrations
        WHERE brand_id = %s
    """
    params: list[Any] = [str(brand_id)]
    if registration_type:
        sql += " AND registration_type = %s"
        params.append(registration_type)
    if registration_status:
        sql += " AND status = %s"
        params.append(registration_status)
    sql += " ORDER BY created_at DESC"

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
            cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in rows]


@router.get("/brands/{brand_id}/registrations/{registration_id}")
async def get_registration_endpoint(
    brand_id: UUID,
    registration_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    reg = await trust_hub_service.get_registration(brand_id, registration_id)
    if reg is None:
        raise HTTPException(status_code=404, detail={"error": "registration_not_found"})
    return reg


@router.post("/brands/{brand_id}/registrations/{registration_id}/refresh")
async def refresh_registration(
    brand_id: UUID,
    registration_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_twilio_creds(brand_id)
    try:
        reg = await trust_hub_service.refresh_registration_status(
            brand_id=brand_id,
            registration_id=registration_id,
            account_sid=creds.account_sid,
            auth_token=creds.auth_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except TwilioProviderError as exc:
        _raise_provider_error("refresh_registration", exc)
    return reg


@router.post("/brands/{brand_id}/phone-numbers/assign", response_model=PhoneNumberAssignmentResponse)
async def assign_phone_number(
    brand_id: UUID,
    body: AssignPhoneNumberRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> PhoneNumberAssignmentResponse:
    """Assign a Twilio phone number to one or more Trust Hub bundles."""
    creds = await _resolve_twilio_creds(brand_id)

    bundle_types = list(body.bundle_types)
    if "customer_profile" in bundle_types:
        bundle_types.remove("customer_profile")
        bundle_types.insert(0, "customer_profile")

    from app.db import get_db_connection

    assignments: list[PhoneNumberAssignment] = []
    for bt in bundle_types:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT bundle_sid FROM trust_hub_registrations
                    WHERE brand_id = %s AND registration_type = %s
                    LIMIT 1
                    """,
                    (str(brand_id), bt),
                )
                row = await cur.fetchone()

        bundle_sid = row[0] if row is not None else None
        if not bundle_sid:
            assignments.append(PhoneNumberAssignment(
                bundle_type=bt,
                status="skipped",
                reason="No registration or bundle_sid not available",
            ))
            continue

        bundle_type_param = "CustomerProfiles" if bt == "customer_profile" else "TrustProducts"
        try:
            result = trust_hub_service.assign_phone_number_to_bundle(
                account_sid=creds.account_sid,
                auth_token=creds.auth_token,
                phone_number_sid=body.phone_number_sid,
                bundle_sid=bundle_sid,
                bundle_type=bundle_type_param,
            )
            assignments.append(PhoneNumberAssignment(
                bundle_type=bt,
                bundle_sid=bundle_sid,
                status="assigned",
                assignment_sid=result.get("sid"),
            ))
        except TwilioProviderError as exc:
            assignments.append(PhoneNumberAssignment(
                bundle_type=bt,
                bundle_sid=bundle_sid,
                status="failed",
                error=str(exc),
            ))

    return PhoneNumberAssignmentResponse(
        phone_number_sid=body.phone_number_sid,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# Webhook endpoint — Twilio Trust Hub status callbacks
#
# Drift fix §7.1: validate X-Twilio-Signature against the brand's auth_token.
# OEX accepted these unsigned. Production should refuse on mismatch
# (TWILIO_WEBHOOK_SIGNATURE_MODE=enforce).
# ---------------------------------------------------------------------------


@webhook_router.post("/api/webhooks/twilio-trust-hub/{brand_id}")
async def trust_hub_status_callback(brand_id: UUID, request: Request) -> Response:
    form_data = await request.form()
    params = {k: v for k, v in form_data.items()}

    # Signature validation.
    sig_mode = settings.TWILIO_WEBHOOK_SIGNATURE_MODE
    if sig_mode != "disabled":
        try:
            creds = await brands_svc.get_twilio_creds(brand_id)
        except brands_svc.BrandCredsKeyMissing:
            creds = None
        if creds is None:
            if sig_mode == "enforce":
                raise HTTPException(status_code=403, detail="brand_creds_unavailable")
        else:
            signature = request.headers.get("X-Twilio-Signature", "")
            url = reconstruct_public_url(request)
            valid = validate_twilio_signature(
                auth_token=creds.auth_token,
                url=url,
                params=params,
                signature=signature,
            )
            if not valid:
                if sig_mode == "enforce":
                    raise HTTPException(status_code=403, detail="signature_invalid")

    bundle_sid = (
        form_data.get("CustomerProfileSid")
        or form_data.get("TrustProductSid")
        or form_data.get("Sid")
    )
    new_status = form_data.get("Status")
    if not bundle_sid or not new_status:
        return Response(status_code=200)

    await trust_hub_service.apply_callback_status_update(
        bundle_sid=str(bundle_sid),
        new_status=str(new_status),
        twilio_payload=params,
    )
    return Response(status_code=200)
