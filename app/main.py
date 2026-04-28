import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import close_pool, init_pool
from app.routers import brands as brands_router
from app.routers import health
from app.routers import phone_numbers as phone_numbers_router
from app.routers import sms as sms_router
from app.routers import trust_hub as trust_hub_router
from app.routers import twilio_webhooks as twilio_webhooks_router
from app.routers import vapi_webhooks as vapi_webhooks_router
from app.routers import voice_ai as voice_ai_router
from app.routers import voice_inbound as voice_inbound_router
from app.routers.admin import me as admin_me
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
app.include_router(admin_me.router, prefix="/admin")
app.include_router(brands_router.router)
app.include_router(trust_hub_router.router)
app.include_router(trust_hub_router.webhook_router)
app.include_router(phone_numbers_router.router)
app.include_router(voice_ai_router.router)
app.include_router(voice_inbound_router.router)
app.include_router(sms_router.router)
app.include_router(vapi_webhooks_router.router)
app.include_router(twilio_webhooks_router.router)
