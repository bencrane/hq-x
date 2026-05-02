import hmac

from fastapi import HTTPException, Request, status

from app.config import settings


def verify_trigger_secret(request: Request) -> None:
    """Verify Authorization: Bearer <TRIGGER_SHARED_SECRET>.

    Used by /internal/* routes that are called by Trigger.dev tasks. The
    same secret value lives in hq-x's Doppler and in the Trigger.dev
    project's env vars. No JWT, no JWKS — just a static shared secret.
    """
    configured = settings.TRIGGER_SHARED_SECRET
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "internal_auth_failed",
                "reason": "trigger_secret_not_configured",
                "message": "TRIGGER_SHARED_SECRET not set",
            },
        )

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "internal_auth_failed",
                "reason": "invalid_trigger_secret",
                "message": "Missing or malformed Authorization header",
            },
        )

    presented = header[len("Bearer ") :]
    if not hmac.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "internal_auth_failed",
                "reason": "invalid_trigger_secret",
                "message": "Invalid trigger shared secret",
            },
        )
