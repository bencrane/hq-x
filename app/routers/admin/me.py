from fastapi import APIRouter, Depends

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext

router = APIRouter(tags=["admin"])


@router.get("/me")
async def admin_me(user: UserContext = Depends(require_operator)) -> dict[str, str]:
    return {
        "user_id": str(user.auth_user_id),
        "business_user_id": str(user.business_user_id),
        "email": user.email,
        "role": user.role,
    }
