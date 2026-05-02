"""Phone number provisioning — search / purchase / list / get / update / release.

All endpoints scope to a brand. Twilio creds come from the encrypted
brands row via app.services.brands.get_twilio_creds.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import (
    get_phone_number as twilio_get_phone_number,
    list_phone_numbers as twilio_list_phone_numbers,
    purchase_phone_number as twilio_purchase_phone_number,
    release_phone_number as twilio_release_phone_number,
    search_available_numbers as twilio_search_available_numbers,
    update_phone_number as twilio_update_phone_number,
)
from app.services import brands as brands_svc

router = APIRouter(prefix="/api/brands/{brand_id}/phone-numbers", tags=["phone-numbers"])


class PhoneNumberPurchaseRequest(BaseModel):
    phone_number: str
    friendly_name: str | None = None
    voice_application_sid: str | None = None
    sms_url: str | None = None
    model_config = {"extra": "forbid"}


class PhoneNumberUpdateRequest(BaseModel):
    voice_application_sid: str | None = None
    voice_url: str | None = None
    sms_url: str | None = None
    friendly_name: str | None = None
    model_config = {"extra": "forbid"}


async def _resolve_creds(brand_id: UUID) -> brands_svc.BrandTwilioCreds:
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


@router.get("/search")
async def search_numbers(
    brand_id: UUID,
    country_code: str = "US",
    number_type: str = "Local",
    area_code: str | None = None,
    in_region: str | None = None,
    in_postal_code: str | None = None,
    contains: str | None = None,
    sms_enabled: bool | None = None,
    voice_enabled: bool | None = None,
    limit: int = 20,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        result = twilio_search_available_numbers(
            creds.account_sid, creds.auth_token,
            country_code=country_code,
            number_type=number_type,
            area_code=area_code,
            in_region=in_region,
            in_postal_code=in_postal_code,
            contains=contains,
            sms_enabled=sms_enabled,
            voice_enabled=voice_enabled,
            limit=limit,
        )
    except TwilioProviderError as exc:
        _raise_provider_error("search_available_numbers", exc)
    return {"provider": "twilio", "result": result}


@router.post("/purchase")
async def purchase_number(
    brand_id: UUID,
    body: PhoneNumberPurchaseRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Purchase a number on Twilio AND record it in voice_phone_numbers for the brand."""
    creds = await _resolve_creds(brand_id)
    try:
        twilio_result = twilio_purchase_phone_number(
            creds.account_sid, creds.auth_token,
            phone_number=body.phone_number,
            voice_application_sid=body.voice_application_sid,
            sms_url=body.sms_url,
            friendly_name=body.friendly_name,
        )
    except TwilioProviderError as exc:
        _raise_provider_error("purchase_phone_number", exc)

    twilio_phone_number_sid = twilio_result.get("sid")
    twilio_phone_number = twilio_result.get("phone_number") or body.phone_number

    from app.db import get_db_connection
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO voice_phone_numbers (
                    brand_id, phone_number, twilio_phone_number_sid,
                    provider, purpose, status
                )
                VALUES (%s, %s, %s, 'twilio', 'both', 'active')
                RETURNING id
                """,
                (str(brand_id), twilio_phone_number, twilio_phone_number_sid),
            )
            row = await cur.fetchone()
        await conn.commit()

    return {
        "provider": "twilio",
        "voice_phone_number_id": row[0],
        "twilio_phone_number_sid": twilio_phone_number_sid,
        "phone_number": twilio_phone_number,
        "twilio_response": twilio_result,
    }


@router.get("")
async def list_numbers(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List voice_phone_numbers rows for the brand."""
    from app.db import get_db_connection
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, phone_number, twilio_phone_number_sid,
                       vapi_phone_number_id, voice_assistant_id,
                       label, purpose, status, created_at, updated_at
                FROM voice_phone_numbers
                WHERE brand_id = %s AND deleted_at IS NULL
                ORDER BY created_at
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


@router.get("/{voice_phone_number_id}/twilio")
async def get_twilio_number(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Fetch Twilio's view of one of this brand's numbers."""
    creds = await _resolve_creds(brand_id)

    from app.db import get_db_connection
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT twilio_phone_number_sid FROM voice_phone_numbers
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail={"error": "phone_number_not_found"})

    try:
        result = twilio_get_phone_number(
            creds.account_sid, creds.auth_token,
            phone_number_sid=row[0],
        )
    except TwilioProviderError as exc:
        _raise_provider_error("get_phone_number", exc)
    return {"provider": "twilio", "result": result}


@router.patch("/{voice_phone_number_id}/twilio")
async def update_twilio_number(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    body: PhoneNumberUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)

    from app.db import get_db_connection
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT twilio_phone_number_sid FROM voice_phone_numbers
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail={"error": "phone_number_not_found"})

    try:
        result = twilio_update_phone_number(
            creds.account_sid, creds.auth_token,
            phone_number_sid=row[0],
            voice_application_sid=body.voice_application_sid,
            voice_url=body.voice_url,
            sms_url=body.sms_url,
            friendly_name=body.friendly_name,
        )
    except TwilioProviderError as exc:
        _raise_provider_error("update_phone_number", exc)
    return {"provider": "twilio", "result": result}


@router.delete("/{voice_phone_number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_number(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    """Release on Twilio + soft-delete locally."""
    creds = await _resolve_creds(brand_id)

    from app.db import get_db_connection
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT twilio_phone_number_sid FROM voice_phone_numbers
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_number_not_found"})

    if row[0]:
        try:
            twilio_release_phone_number(
                creds.account_sid, creds.auth_token,
                phone_number_sid=row[0],
            )
        except TwilioProviderError as exc:
            _raise_provider_error("release_phone_number", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_phone_numbers
                SET deleted_at = NOW(), updated_at = NOW(), status = 'inactive'
                WHERE id = %s AND brand_id = %s
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
        await conn.commit()


@router.get("/twilio/inventory")
async def list_twilio_inventory(
    brand_id: UUID,
    phone_number: str | None = None,
    friendly_name: str | None = None,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """List the Twilio account's view of purchased numbers (regardless of local rows)."""
    creds = await _resolve_creds(brand_id)
    try:
        result = twilio_list_phone_numbers(
            creds.account_sid, creds.auth_token,
            phone_number=phone_number,
            friendly_name=friendly_name,
        )
    except TwilioProviderError as exc:
        _raise_provider_error("list_phone_numbers", exc)
    return {"provider": "twilio", "result": result}
