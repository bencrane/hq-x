import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import assert_production_safe, settings
from app.db import close_pool, init_pool
from app.mcp.bearer_auth import bearer_token_app
from app.mcp.dmaas import mcp as dmaas_mcp
from app.routers import audience_drafts as audience_drafts_router
from app.routers import brands as brands_router
from app.routers import campaigns as campaigns_router
from app.routers import channel_campaign_steps as channel_campaign_steps_router
from app.routers import channel_campaigns as channel_campaigns_router
from app.routers import direct_mail as direct_mail_router
from app.routers import dmaas as dmaas_router
from app.routers import dub as dub_router
from app.routers import entri as entri_router
from app.routers import health
from app.routers import ivr as ivr_router
from app.routers import ivr_config as ivr_config_router
from app.routers import outbound_calls as outbound_calls_router
from app.routers import phone_numbers as phone_numbers_router
from app.routers import sms as sms_router
from app.routers import trust_hub as trust_hub_router
from app.routers import twilio_webhooks as twilio_webhooks_router
from app.routers import twiml_apps as twiml_apps_router
from app.routers import vapi_analytics as vapi_analytics_router
from app.routers import vapi_calls as vapi_calls_router
from app.routers import vapi_campaigns as vapi_campaigns_router
from app.routers import vapi_files as vapi_files_router
from app.routers import vapi_insights as vapi_insights_router
from app.routers import vapi_knowledge_bases as vapi_knowledge_bases_router
from app.routers import vapi_phone_numbers as vapi_phone_numbers_router
from app.routers import vapi_squads as vapi_squads_router
from app.routers import vapi_tools as vapi_tools_router
from app.routers import vapi_webhooks as vapi_webhooks_router
from app.routers import voice as voice_router
from app.routers import voice_ai as voice_ai_router
from app.routers import voice_analytics as voice_analytics_router
from app.routers import voice_campaigns as voice_campaigns_router
from app.routers import voice_inbound as voice_inbound_router
from app.routers.admin import me as admin_me
from app.routers.internal import emailbison as internal_emailbison
from app.routers.internal import scheduler as internal_scheduler
from app.routers.internal import voice_callbacks as internal_voice_callbacks
from app.routers.webhooks import cal as cal_webhooks
from app.routers.webhooks import dub as dub_webhooks
from app.routers.webhooks import emailbison as emailbison_webhooks
from app.routers.webhooks import entri as entri_webhooks
from app.routers.webhooks import lob as lob_webhooks

# FastMCP exposes its tools via an ASGI sub-app at /mcp; the sub-app has
# its own lifespan we have to chain in so MCP's session manager starts up.
# Wrap in a Bearer-token check so managed agents authenticate at the MCP
# transport boundary. Token comes from DMAAS_MCP_BEARER_TOKEN; required in
# prd, optional in dev (see assert_production_safe).
_dmaas_mcp_inner = dmaas_mcp.http_app(path="/")
_dmaas_mcp_app = bearer_token_app(
    _dmaas_mcp_inner,
    bearer_token=(
        settings.DMAAS_MCP_BEARER_TOKEN.get_secret_value()
        if settings.DMAAS_MCP_BEARER_TOKEN
        else None
    ),
)


@asynccontextmanager
async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
    assert_production_safe(settings)
    await init_pool()
    async with _dmaas_mcp_inner.lifespan(app_):
        try:
            yield
        finally:
            await close_pool()


logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = FastAPI(title="hq-x", lifespan=lifespan)
# Mount the DMaaS MCP server at /mcp/dmaas. Managed agents authenticate
# via Authorization: Bearer <DMAAS_MCP_BEARER_TOKEN>; the wrapper rejects
# unauthorized requests at the ASGI boundary before FastMCP sees them.
app.mount("/mcp/dmaas", _dmaas_mcp_app)


# ---------------------------------------------------------------------------
# Error handlers — make 500s actionable instead of bare null bodies.
#
# FastAPI's default for an uncaught exception is HTTP 500 with no JSON
# body, which is useless for a frontend trying to display a meaningful
# error. We register handlers that return a structured envelope matching
# the shape used by HTTPException raises elsewhere:
#     {"detail": {"error": "...", "message": "...", "request_id": "..."}}
#
# Full traceback always goes to the server log, keyed by request_id, so
# operators can grep `request_id=abcd...` to find the offending stack.
# ---------------------------------------------------------------------------


def _safe_message(exc: BaseException, max_len: int = 500) -> str:
    """Truncate exception text for the response body. Avoid leaking
    full stack frames; the operator should look at server logs instead.
    """
    text = str(exc) or type(exc).__name__
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


@app.exception_handler(psycopg.Error)
async def psycopg_error_handler(request: Request, exc: psycopg.Error) -> JSONResponse:
    """Database errors are usually transient (pool exhausted, connection
    reset, statement timeout). Map to 503 so callers know to retry.
    """
    request_id = uuid.uuid4().hex
    logger.exception(
        "psycopg_error request_id=%s method=%s path=%s",
        request_id, request.method, request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error": "database_error",
                "type": type(exc).__name__,
                "message": _safe_message(exc),
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "retryable": True,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    """FastAPI's default 422 response is verbose; flatten the per-field
    errors into a list and wrap in our standard envelope.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error": "validation_error",
                "message": "Request validation failed.",
                "errors": jsonable_encoder(exc.errors()),
                "method": request.method,
                "path": request.url.path,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, exc: Exception,
) -> JSONResponse:
    """Catch-all for anything not handled by a more specific handler.

    Note: ``HTTPException`` is handled by FastAPI's built-in handler
    BEFORE this catch-all runs, so explicit ``raise HTTPException(...)``
    paths keep their existing 4xx envelope. This handler only fires for
    genuinely uncaught exceptions (typos, AttributeError, etc.).
    """
    request_id = uuid.uuid4().hex
    logger.exception(
        "unhandled_exception request_id=%s method=%s path=%s",
        request_id, request.method, request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "error": "internal_server_error",
                "type": type(exc).__name__,
                "message": _safe_message(exc),
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            }
        },
    )


app.include_router(health.router)
app.include_router(cal_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(emailbison_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(lob_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(dub_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(entri_webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(internal_scheduler.router, prefix="/internal")
app.include_router(internal_emailbison.router, prefix="/internal")
app.include_router(internal_voice_callbacks.router, prefix="/internal")
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
app.include_router(direct_mail_router.router)
app.include_router(dmaas_router.router)
app.include_router(dub_router.router)
app.include_router(entri_router.router)
app.include_router(ivr_router.router)
app.include_router(ivr_config_router.router)
app.include_router(twiml_apps_router.router)
app.include_router(outbound_calls_router.router)
app.include_router(vapi_analytics_router.router)
app.include_router(vapi_calls_router.router)
app.include_router(vapi_campaigns_router.router)
app.include_router(vapi_files_router.router)
app.include_router(vapi_insights_router.router)
app.include_router(vapi_knowledge_bases_router.router)
app.include_router(vapi_phone_numbers_router.router)
app.include_router(vapi_squads_router.router)
app.include_router(vapi_tools_router.router)
app.include_router(voice_router.router)
app.include_router(voice_campaigns_router.router)
app.include_router(voice_analytics_router.router)
app.include_router(audience_drafts_router.router)
app.include_router(campaigns_router.router)
app.include_router(channel_campaigns_router.router)
app.include_router(channel_campaign_steps_router.nested_router)
app.include_router(channel_campaign_steps_router.flat_router)
