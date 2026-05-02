import hmac

from fastapi import HTTPException, Request, status

from app.config import settings


def _request_origin_host(request: Request) -> str | None:
    # Precedence: Origin → Referer → X-Forwarded-Host → Host
    origin = request.headers.get("Origin")
    if origin:
        return origin.split("://", 1)[-1].lower()

    referer = request.headers.get("Referer")
    if referer:
        return referer.split("://", 1)[-1].split("/", 1)[0].lower()

    forwarded_host = request.headers.get("X-Forwarded-Host")
    if forwarded_host:
        return forwarded_host.lower()

    host = request.headers.get("Host")
    if host:
        return host.split(":")[0].lower()

    return None


def _allowed_origin_hosts() -> set[str] | None:
    raw = settings.EMAILBISON_WEBHOOK_ALLOWED_ORIGINS
    if not raw:
        return None
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def verify_emailbison_trust(request: Request, path_token: str) -> str:
    """Verify path token + origin allowlist. Returns the resolved origin host."""
    configured = settings.EMAILBISON_WEBHOOK_PATH_TOKEN
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "webhook_auth_failed",
                "provider": "emailbison",
                "reason": "path_token_not_configured",
                "message": "EMAILBISON_WEBHOOK_PATH_TOKEN not set",
            },
        )
    if not hmac.compare_digest(path_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "webhook_auth_failed",
                "provider": "emailbison",
                "reason": "invalid_path_token",
                "message": "Invalid EmailBison webhook path token",
            },
        )

    origin_host = _request_origin_host(request)
    allowlist = _allowed_origin_hosts()
    if not origin_host:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "webhook_auth_failed",
                "provider": "emailbison",
                "reason": "missing_origin_header",
                "message": "Request missing Origin or equivalent header",
            },
        )
    if not allowlist or origin_host not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "webhook_auth_failed",
                "provider": "emailbison",
                "reason": "disallowed_origin",
                "message": f"Origin '{origin_host}' not in allowlist",
            },
        )
    return origin_host
