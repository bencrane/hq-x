import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import close_pool, init_pool
from app.routers import health
from app.routers.internal import scheduler as internal_scheduler
from app.routers.webhooks import cal as cal_webhooks
from app.routers.webhooks import emailbison as emailbison_webhooks


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


logging.basicConfig(level=settings.LOG_LEVEL)

app = FastAPI(title="hq-x", lifespan=lifespan)
app.include_router(health.router)
app.include_router(cal_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(emailbison_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(internal_scheduler.router, prefix="/internal")
