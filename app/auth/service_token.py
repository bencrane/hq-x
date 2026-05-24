"""Verify `Authorization: Bearer <BACKEND_X_SERVICE_TOKEN>`.

Used by routes that the hq-zone platform-api BFF calls into. The same
secret value lives in Doppler hq-zone/prd (outbound, used to sign
calls) and Doppler hq-all/prd (expected, used here to verify). No JWT,
no JWKS — just a static shared secret.

Modeled on `app/auth/trigger_secret.py`. Kept deliberately separate so
the two surfaces can rotate independently.
"""

import hmac

from fastapi import HTTPException, Request, status

from app.config import settings


def verify_backend_x_token(request: Request) -> None:
    configured = settings.BACKEND_X_SERVICE_TOKEN
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "backend_x_auth_failed",
                "reason": "service_token_not_configured",
                "message": "BACKEND_X_SERVICE_TOKEN not set",
            },
        )

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "backend_x_auth_failed",
                "reason": "invalid_service_token",
                "message": "Missing or malformed Authorization header",
            },
        )

    presented = header[len("Bearer ") :]
    if not hmac.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "backend_x_auth_failed",
                "reason": "invalid_service_token",
                "message": "Invalid backend-x service token",
            },
        )
