"""TwiML Application CRUD against Twilio's REST API, brand-scoped via path.

Mirrors the equivalent OEX endpoints but uses brand-encrypted creds and the
``require_flexible_auth`` dependency.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import (
    create_application,
    delete_application,
    get_application,
    list_applications,
    update_application,
)
from app.services import brands as brands_svc

router = APIRouter(prefix="/api/brands/{brand_id}/twiml-apps", tags=["twiml-apps"])


class TwimlAppCreateRequest(BaseModel):
    friendly_name: str
    voice_url: str | None = None
    voice_method: str | None = None
    voice_fallback_url: str | None = None
    status_callback: str | None = None
    status_callback_method: str | None = None
    model_config = {"extra": "forbid"}


class TwimlAppUpdateRequest(BaseModel):
    friendly_name: str | None = None
    voice_url: str | None = None
    voice_method: str | None = None
    voice_fallback_url: str | None = None
    status_callback: str | None = None
    status_callback_method: str | None = None
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


def _raise_provider_http_error(operation: str, exc: TwilioProviderError) -> None:
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_502_BAD_GATEWAY
    )
    raise HTTPException(
        status_code=http_status,
        detail={
            "type": "provider_error",
            "provider": "twilio",
            "operation": operation,
            "retryable": exc.retryable,
            "message": str(exc),
        },
    ) from exc


@router.post("")
async def create_twiml_app(
    brand_id: UUID,
    body: TwimlAppCreateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        result = create_application(
            creds.account_sid,
            creds.auth_token,
            friendly_name=body.friendly_name,
            voice_url=body.voice_url,
            voice_method=body.voice_method,
            voice_fallback_url=body.voice_fallback_url,
            status_callback=body.status_callback,
            status_callback_method=body.status_callback_method,
        )
    except TwilioProviderError as exc:
        _raise_provider_http_error("create_application", exc)
    return {"provider": "twilio", "result": result}


@router.get("")
async def list_twiml_apps(
    brand_id: UUID,
    friendly_name: str | None = None,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        result = list_applications(
            creds.account_sid, creds.auth_token, friendly_name=friendly_name,
        )
    except TwilioProviderError as exc:
        _raise_provider_http_error("list_applications", exc)
    return {"provider": "twilio", "result": result}


@router.get("/{app_sid}")
async def get_twiml_app(
    brand_id: UUID,
    app_sid: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        result = get_application(
            creds.account_sid, creds.auth_token, application_sid=app_sid,
        )
    except TwilioProviderError as exc:
        _raise_provider_http_error("get_application", exc)
    return {"provider": "twilio", "result": result}


@router.post("/{app_sid}")
async def update_twiml_app(
    brand_id: UUID,
    app_sid: str,
    body: TwimlAppUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        result = update_application(
            creds.account_sid,
            creds.auth_token,
            application_sid=app_sid,
            friendly_name=body.friendly_name,
            voice_url=body.voice_url,
            voice_method=body.voice_method,
            voice_fallback_url=body.voice_fallback_url,
            status_callback=body.status_callback,
            status_callback_method=body.status_callback_method,
        )
    except TwilioProviderError as exc:
        _raise_provider_http_error("update_application", exc)
    return {"provider": "twilio", "result": result}


@router.delete("/{app_sid}")
async def delete_twiml_app(
    brand_id: UUID,
    app_sid: str,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    creds = await _resolve_creds(brand_id)
    try:
        delete_application(
            creds.account_sid, creds.auth_token, application_sid=app_sid,
        )
    except TwilioProviderError as exc:
        _raise_provider_http_error("delete_application", exc)
    return {"provider": "twilio", "deleted": True, "application_sid": app_sid}
