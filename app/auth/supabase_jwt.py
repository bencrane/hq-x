from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings
from app.db import get_db_connection

SUPABASE_JWT_ALGORITHM = "ES256"
SUPABASE_JWT_AUDIENCE = "authenticated"

_jwk_client: jwt.PyJWKClient | None = None


def _jwks_url() -> str:
    base = str(settings.HQX_SUPABASE_URL).rstrip("/")
    return f"{base}/auth/v1/.well-known/jwks.json"


def _jwk_client_singleton() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(_jwks_url(), cache_keys=True, lifespan=600)
    return _jwk_client


def _get_signing_key(token: str) -> Any:
    """Resolve the public key for `token` from Supabase's JWKS endpoint.

    Indirection point: tests monkeypatch this to return a local EC public key.
    """
    return _jwk_client_singleton().get_signing_key_from_jwt(token).key


@dataclass(frozen=True)
class UserContext:
    auth_user_id: UUID
    business_user_id: UUID
    email: str
    role: str
    client_id: UUID | None


def _unauthorized(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": error},
    )


def _forbidden(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": error},
    )


def _decode_jwt(token: str) -> dict[str, Any]:
    try:
        signing_key = _get_signing_key(token)
    except jwt.PyJWKClientError as exc:
        raise _unauthorized("malformed_token") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("malformed_token") from exc

    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=[SUPABASE_JWT_ALGORITHM],
            audience=SUPABASE_JWT_AUDIENCE,
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("token_expired") from exc
    except jwt.InvalidSignatureError as exc:
        raise _unauthorized("invalid_signature") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("malformed_token") from exc


async def _lookup_business_user(auth_user_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, email, role, client_id
                FROM business.users
                WHERE auth_user_id = %s
                """,
                (str(auth_user_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "role": row[2],
        "client_id": row[3],
    }


async def verify_supabase_jwt(request: Request) -> UserContext:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise _unauthorized("missing_auth")

    token = header[len("Bearer ") :].strip()
    if not token:
        raise _unauthorized("missing_auth")

    claims = _decode_jwt(token)

    sub = claims.get("sub")
    if not sub:
        raise _unauthorized("malformed_token")
    try:
        auth_user_id = UUID(sub)
    except ValueError as exc:
        raise _unauthorized("malformed_token") from exc

    row = await _lookup_business_user(auth_user_id)
    if row is None:
        raise _forbidden("user_not_provisioned")

    return UserContext(
        auth_user_id=auth_user_id,
        business_user_id=row["id"],
        email=row["email"],
        role=row["role"],
        client_id=row["client_id"],
    )
