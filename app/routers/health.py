from fastapi import APIRouter

from app.config import settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}
