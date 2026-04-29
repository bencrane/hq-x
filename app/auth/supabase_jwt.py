from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings
from app.db import get_db_connection

SUPABASE_JWT_ALGORITHM = "ES256"
SUPABASE_JWT_AUDIENCE = "authenticated"

ORGANIZATION_HEADER = "X-Organization-Id"

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
    # Two-axis roles. platform_role is global ('platform_operator' or None).
    # org_role is scoped to active_organization_id.
    platform_role: str | None
    active_organization_id: UUID | None
    org_role: str | None
    # Legacy fields preserved during transition. To be deprecated once all
    # callers move off business.users.role / business.users.client_id.
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


def _bad_request(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
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
                SELECT id, email, role, client_id, platform_role
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
        "platform_role": row[4],
    }


async def _lookup_memberships(business_user_id: UUID) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT organization_id, org_role
                FROM business.organization_memberships
                WHERE user_id = %s AND status = 'active'
                """,
                (str(business_user_id),),
            )
            rows = await cur.fetchall()
    return [{"organization_id": r[0], "org_role": r[1]} for r in rows]


def _parse_org_header(request: Request) -> UUID | None:
    raw = request.headers.get(ORGANIZATION_HEADER)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise _bad_request("invalid_organization_id") from exc


async def _resolve_org_context(
    request: Request,
    business_user_id: UUID,
    is_platform_operator: bool,
) -> tuple[UUID | None, str | None]:
    """Resolve (active_organization_id, org_role).

    Rules (per directive):
      * If X-Organization-Id is provided, validate membership (or platform
        operator bypass), and use it.
      * Else if user has exactly one active membership, use that.
      * Else leave both None; endpoints requiring an org will 400.
    """
    requested = _parse_org_header(request)
    memberships = await _lookup_memberships(business_user_id)

    if requested is not None:
        for m in memberships:
            if m["organization_id"] == requested:
                return requested, m["org_role"]
        if is_platform_operator:
            return requested, None  # cross-org access; no org_role
        raise _forbidden("not_a_member_of_organization")

    if len(memberships) == 1:
        only = memberships[0]
        return only["organization_id"], only["org_role"]

    return None, None


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

    is_platform_operator = row["platform_role"] == "platform_operator"
    active_org_id, org_role = await _resolve_org_context(
        request,
        row["id"],
        is_platform_operator,
    )

    return UserContext(
        auth_user_id=auth_user_id,
        business_user_id=row["id"],
        email=row["email"],
        platform_role=row["platform_role"],
        active_organization_id=active_org_id,
        org_role=org_role,
        role=row["role"],
        client_id=row["client_id"],
    )
