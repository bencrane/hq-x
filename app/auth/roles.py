from collections.abc import Callable, Iterable

from fastapi import Depends, HTTPException, status

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt


def require_platform_operator(
    user: UserContext = Depends(verify_supabase_jwt),
) -> UserContext:
    if user.platform_role != "platform_operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "platform_operator_required"},
        )
    return user


def require_org_context(
    user: UserContext = Depends(verify_supabase_jwt),
) -> UserContext:
    if user.active_organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "organization_context_required"},
        )
    return user


def require_org_role(*allowed_roles: str) -> Callable[..., UserContext]:
    """Dependency factory: require the active org_role to be in `allowed_roles`.

    Platform operators bypass the org_role check (they may not have a
    membership in the targeted org), but still need an active_organization_id
    — set via the X-Organization-Id header.
    """
    allowed: tuple[str, ...] = tuple(allowed_roles)

    def _check(user: UserContext = Depends(require_org_context)) -> UserContext:
        if user.platform_role == "platform_operator":
            return user
        if user.org_role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "insufficient_org_role",
                    "allowed": list(allowed),
                },
            )
        return user

    return _check


# ── Backward-compat shims ────────────────────────────────────────────────
#
# Existing routers use `require_operator` for what is, under the new model,
# platform-operator-only access (admin/me, dmaas scaffold authoring, dub
# write ops, lob webhooks, all of direct_mail). Map it to the new dependency
# rather than duplicating call sites.
#
# `require_client` was unused at directive time; preserved for source compat.
require_operator = require_platform_operator


def require_client(
    user: UserContext = Depends(verify_supabase_jwt),
) -> UserContext:
    if user.role != "client":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "client_role_required"},
        )
    return user


__all__: Iterable[str] = (
    "require_platform_operator",
    "require_org_context",
    "require_org_role",
    "require_operator",
    "require_client",
)
