"""`GET /api/me` — identity probe behind the hybrid edge dependency.

Proves both doors of app/auth/hybrid.py: a valid Supabase JWT (direct
browser client) and a valid X-Service-Token (internal service) each pass
the gate. The body echoes which door authorized the request.
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.hybrid import AuthPrincipal, authenticate

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/me")
async def me(principal: AuthPrincipal = Depends(authenticate)) -> dict[str, Any]:
    return {
        "kind": principal.kind,
        "user_id": str(principal.user_id) if principal.user_id else None,
        "email": (principal.claims or {}).get("email"),
    }
