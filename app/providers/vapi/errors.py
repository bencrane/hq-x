"""Shared Vapi error-mapping helpers.

Extracted from app/routers/voice_ai.py so every Vapi-facing router maps
provider failures the same way: 503 for transient (retryable), 502 for
terminal (non-retryable). Behavior is unchanged from the inline copy.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException, status

from app.config import settings
from app.providers.vapi._http import VapiProviderError


def vapi_key() -> str:
    if settings.VAPI_API_KEY is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "VAPI_API_KEY not configured"},
        )
    return settings.VAPI_API_KEY.get_secret_value()


def raise_vapi_error(operation: str, exc: VapiProviderError) -> NoReturn:
    code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.retryable
        else status.HTTP_502_BAD_GATEWAY
    )
    raise HTTPException(
        status_code=code,
        detail={
            "type": "provider_error",
            "provider": "vapi",
            "operation": operation,
            "retryable": exc.retryable,
            "message": str(exc),
        },
    ) from exc
