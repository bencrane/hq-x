"""require_flexible_auth — accepts either an operator JWT or the trigger secret.

In single-operator world there are two legitimate callers:
  1. The operator (Ben) — Supabase ES256 JWT verified via JWKS, role='operator'.
  2. System callers (Trigger.dev tasks, internal jobs) — bear TRIGGER_SHARED_SECRET.

The directive's "super-admin API key OR super-admin JWT OR Supabase ES256 JWT"
collapses onto these two in the current hq-x deployment.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request, status

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.config import settings


@dataclass(frozen=True)
class SystemContext:
    """Identity context for a system caller authenticated by TRIGGER_SHARED_SECRET."""

    kind: Literal["system"] = "system"


FlexibleContext = UserContext | SystemContext


def _trigger_secret_matches(request: Request) -> bool:
    configured = settings.TRIGGER_SHARED_SECRET
    if not configured:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    presented = header[len("Bearer ") :].strip()
    return hmac.compare_digest(presented, configured)


async def require_flexible_auth(request: Request) -> FlexibleContext:
    """Accept either operator JWT or the Trigger shared secret.

    Order: try the trigger-secret comparison first (cheap, constant-time, no
    DB hop). If it doesn't match, fall through to JWT verification which can
    raise 401 / 403 with its own error shape.
    """
    if _trigger_secret_matches(request):
        return SystemContext()

    user = await verify_supabase_jwt(request)
    if user.platform_role != "platform_operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "platform_operator_required"},
        )
    return user
