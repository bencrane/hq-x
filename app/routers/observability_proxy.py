"""Observability proxy — forwards DEX source-state calls to hq-command.

GET /api/v1/observability/sources
  → DEX GET /api/v1/internal/observability/sources
  Returns list[SourceStateRow] (shapes forwarded verbatim from DEX).

Auth: verify_supabase_jwt (hq-x Supabase ES256 JWT). The user JWT is
forwarded to DEX; DEX's require_flexible_auth accepts it via hq-x Supabase
JWKS validation. Fallback to DEX_SUPER_ADMIN_API_KEY when no user JWT is
present on the DEX client (server-to-server path used by hq-command's
Next.js server components and curl-level monitoring).

Architecture: hq-command calls hq-x; hq-x calls DEX. hq-command never
calls DEX directly per app_responsibilities.md.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.services import dex_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])


@router.get("/sources")
async def get_observability_sources(
    request: Request,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> list[dict[str, Any]]:
    """Return freshness state for all registered DEX data sources.

    Thin proxy — shape and breach semantics are owned by DEX.
    """
    # Forward the caller's Bearer token so DEX sees the hq-x user identity.
    auth_header = request.headers.get("Authorization", "")
    bearer_token: str | None = None
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:]

    try:
        result = await dex_client._request(
            "GET",
            "/api/v1/internal/observability/sources",
            bearer_token=bearer_token,
        )
    except dex_client.DexNotConfiguredError as exc:
        log.error("DEX not configured: %s", exc)
        raise HTTPException(status_code=503, detail="observability upstream not configured") from exc
    except dex_client.DexAuthMissingError as exc:
        log.error("DEX auth missing: %s", exc)
        raise HTTPException(status_code=503, detail="observability upstream auth not configured") from exc
    except dex_client.DexCallError as exc:
        log.warning("DEX observability/sources failed: status=%s", exc.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"observability upstream error: {exc.status_code}",
        ) from exc

    # DEX returns a plain list (not the {"data": ...} envelope) for this endpoint.
    # _request/_unwrap handles the envelope case; if we got a list back, return it.
    if isinstance(result, list):
        return result
    # Fallback: DEX returned an unexpected shape — surface it as-is.
    return result  # type: ignore[return-value]
