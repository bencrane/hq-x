"""Hybrid edge authentication for hq-x.

A single FastAPI dependency, ``authenticate``, that admits two kinds of
caller and rejects everything else with 401:

  Door A — internal service. A trusted backend caller (hq-x's own
    Trigger.dev tasks, etc.) presents the shared secret in the
    ``X-Service-Token`` header. If it matches ``HQ_X_SERVICE_TOKEN`` the
    request is authorized as a system principal and no JWT work happens.

  Door B — direct browser client. platform-app presents a Supabase-minted
    JWT in ``Authorization: Bearer <token>``. The signature is verified
    asymmetrically (RS256): the signing key is resolved by ``kid`` from
    Supabase's JWKS endpoint (``HQX_SUPABASE_JWKS_URL``) via PyJWKClient,
    and the ``iss`` (``HQX_SUPABASE_ISSUER``) and ``aud`` claims are
    enforced. The ``sub`` claim becomes the user id.

No Supabase client SDK and no symmetric secret are involved — only the
published public keys ever reach hq-x, so the browser client and any
internal service scale out independently (zero-lock-in).

Tests monkeypatch ``_get_signing_key`` to supply a local public key
without touching the network.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings

SUPABASE_JWT_ALGORITHM = "RS256"
SUPABASE_JWT_AUDIENCE = "authenticated"
SERVICE_TOKEN_HEADER = "X-Service-Token"

_jwk_client: jwt.PyJWKClient | None = None


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "reason": reason},
    )


def _misconfigured(reason: str) -> HTTPException:
    """503 for a server-side auth misconfiguration (vs. a bad credential).

    Returning 401 here would mislead the caller into thinking their token
    was rejected. Mirrors app/auth/service_token.py's 503 on an unset token.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "auth_misconfigured", "reason": reason},
    )


@dataclass(frozen=True)
class AuthPrincipal:
    """Resolved identity returned by the hybrid dependency.

    ``kind`` is ``"service"`` for Door A (no user; ``user_id`` / ``claims``
    are None) or ``"user"`` for Door B (``user_id`` is the Supabase ``sub``
    and ``claims`` carries the verified JWT body).
    """

    kind: Literal["service", "user"]
    user_id: UUID | None = None
    claims: dict[str, Any] | None = None


def _jwk_client_singleton() -> jwt.PyJWKClient:
    """Lazily build the JWKS client against ``HQX_SUPABASE_JWKS_URL``.

    Cached process-wide; PyJWKClient keeps its own keyset cache (``lifespan``
    below), so a Supabase key rotation is picked up without a redeploy.
    """
    global _jwk_client
    if _jwk_client is None:
        url = settings.HQX_SUPABASE_JWKS_URL
        if not url:
            raise _misconfigured("jwks_url_not_configured")
        _jwk_client = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)
    return _jwk_client


def _get_signing_key(token: str) -> Any:
    """Resolve the public key for ``token`` from the JWKS endpoint.

    Indirection point: tests monkeypatch this to return a local public key.
    """
    return _jwk_client_singleton().get_signing_key_from_jwt(token).key


def _service_token_matches(presented: str) -> bool:
    configured = settings.HQ_X_SERVICE_TOKEN
    return bool(configured) and hmac.compare_digest(presented, configured)


def _verify_user_jwt(token: str) -> AuthPrincipal:
    if not settings.HQX_SUPABASE_ISSUER:
        raise _misconfigured("issuer_not_configured")

    try:
        signing_key = _get_signing_key(token)
    except jwt.PyJWKClientError as exc:
        raise _unauthorized("signing_key_unavailable") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("malformed_token") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=[SUPABASE_JWT_ALGORITHM],
            audience=SUPABASE_JWT_AUDIENCE,
            issuer=settings.HQX_SUPABASE_ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("token_expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise _unauthorized("invalid_issuer") from exc
    except jwt.InvalidAudienceError as exc:
        raise _unauthorized("invalid_audience") from exc
    except jwt.InvalidSignatureError as exc:
        raise _unauthorized("invalid_signature") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("malformed_token") from exc

    sub = claims.get("sub")
    if not sub:
        raise _unauthorized("missing_sub")
    try:
        user_id = UUID(sub)
    except (ValueError, TypeError) as exc:
        raise _unauthorized("malformed_sub") from exc

    return AuthPrincipal(kind="user", user_id=user_id, claims=claims)


async def authenticate(request: Request) -> AuthPrincipal:
    """Hybrid edge dependency: Door A (service token) then Door B (JWT).

    A present-but-wrong ``X-Service-Token`` is rejected outright rather than
    falling through to JWT verification — a caller asserting the service door
    must satisfy it.
    """
    service_token = request.headers.get(SERVICE_TOKEN_HEADER)
    if service_token:
        if _service_token_matches(service_token):
            return AuthPrincipal(kind="service")
        raise _unauthorized("invalid_service_token")

    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[len("Bearer ") :].strip()
        if token:
            return _verify_user_jwt(token)

    raise _unauthorized("missing_credentials")
