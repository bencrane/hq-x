from fastapi import Depends, HTTPException, status

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt


def require_operator(
    user: UserContext = Depends(verify_supabase_jwt),
) -> UserContext:
    if user.role != "operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "operator_role_required"},
        )
    return user


def require_client(
    user: UserContext = Depends(verify_supabase_jwt),
) -> UserContext:
    if user.role != "client":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "client_role_required"},
        )
    return user
