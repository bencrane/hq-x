"""Hosted landing page render endpoint.

URL shape: `GET /lp/{step_id}/{short_code}`. Entri Power proxies
recipient traffic from `pages.<brand-domain>.com/<short_code>` to this
backend; the path's `applicationUrl` (configured in
`business.entri_domain_connections.application_url`) ends in
`/lp/<step_id>` so the trailing `/<short_code>` is appended by Entri.

Render flow:
  1. Resolve `(step_id, short_code) → recipient` via dmaas_dub_links.
  2. Resolve step → org/brand/campaign hierarchy via get_step_context.
  3. Resolve brand theme + step.landing_page_config.
  4. Render Jinja2 template with personalization tokens applied.
  5. Fire-and-forget `emit_event("page.viewed", ...)` AND insert one
     row into business.landing_page_views — UNLESS the same hashed IP
     hit this step within LANDING_PAGE_VIEW_DEDUPE_SECONDS, in which
     case we still serve the page but skip both side-effects.

404s render a brand-themed not-found page if the brand is resolvable
(the URL path includes a real step_id), otherwise a plain 404.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.dmaas import landing_page_views
from app.observability import incr_metric
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc
from app.services.analytics import emit_event
from app.services.landing_page_render import (
    LandingPageNotFoundError,
    render_not_found_for_brand,
    resolve_landing_page_render,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["landing-pages"])


def _hash_ip(raw_ip: str | None) -> str:
    if not raw_ip:
        return "noip"
    salted = (raw_ip + settings.LANDING_PAGE_IP_HASH_SALT).encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def _client_ip(request: Request) -> str | None:
    """Best-effort visitor IP.

    Entri Power injects `X-Forwarded-IP` per
    https://developers.entri.com/power.md. Behind any other proxy the
    standard `X-Forwarded-For` is used. The result is hashed before any
    persistence; raw IPs never land in our DB.
    """
    fwd = request.headers.get("x-forwarded-ip")
    if fwd:
        return fwd.split(",")[0].strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return None


def _source_metadata(request: Request, *, ip_hash: str) -> dict[str, Any]:
    return {
        "ip_hash": ip_hash,
        "user_agent": (request.headers.get("user-agent") or "")[:500],
        "referrer": (request.headers.get("referer") or "")[:500],
    }


@router.get("/lp/{step_id}/{short_code}", response_class=HTMLResponse)
async def render_landing_page(
    step_id: UUID,
    short_code: str,
    request: Request,
) -> HTMLResponse:
    try:
        rendered = await resolve_landing_page_render(
            step_id=step_id, short_code=short_code
        )
    except LandingPageNotFoundError as exc:
        logger.info("landing_page.not_found step=%s short=%s err=%s", step_id, short_code, exc)
        incr_metric("landing_page.not_found")
        # Try to recover a brand theme so the 404 still looks branded.
        theme = await _best_effort_brand_theme(step_id)
        return HTMLResponse(
            content=render_not_found_for_brand(theme),
            status_code=404,
        )

    ip_hash = _hash_ip(_client_ip(request))
    deduped = await landing_page_views.has_recent_view_for_ip(
        channel_campaign_step_id=rendered.channel_campaign_step_id,
        ip_hash=ip_hash,
        within_seconds=settings.LANDING_PAGE_VIEW_DEDUPE_SECONDS,
    )

    if not deduped:
        meta = _source_metadata(request, ip_hash=ip_hash)
        try:
            await landing_page_views.insert_view(
                organization_id=rendered.organization_id,
                brand_id=rendered.brand_id,
                campaign_id=rendered.campaign_id,
                channel_campaign_id=rendered.channel_campaign_id,
                channel_campaign_step_id=rendered.channel_campaign_step_id,
                recipient_id=rendered.recipient_id,
                source_metadata=meta,
            )
        except Exception:
            # Fire-and-forget: the page must render even if the analytics
            # insert blows up.
            logger.exception(
                "landing_page.view_insert_failed step=%s recipient=%s",
                rendered.channel_campaign_step_id,
                rendered.recipient_id,
            )
        try:
            await emit_event(
                event_name="page.viewed",
                channel_campaign_step_id=rendered.channel_campaign_step_id,
                recipient_id=rendered.recipient_id,
                properties={
                    "user_agent": meta["user_agent"],
                    "referrer": meta["referrer"],
                    "ip_hash": ip_hash,
                },
            )
        except Exception:
            logger.exception(
                "landing_page.emit_failed step=%s", rendered.channel_campaign_step_id
            )
        incr_metric("landing_page.rendered")
    else:
        incr_metric("landing_page.rendered_dedupe")

    return HTMLResponse(content=rendered.html, status_code=200)


async def _best_effort_brand_theme(step_id: UUID) -> dict[str, Any] | None:
    """Try to resolve the brand theme without an org context.

    Used by the 404 path so unknown short_codes still look branded if
    the step itself is real. Returns None if anything fails.
    """
    try:
        ctx = await steps_svc.get_step_context(step_id=step_id)
    except Exception:
        return None
    if ctx is None:
        return None
    try:
        return await brands_svc.get_theme(UUID(ctx["brand_id"]))
    except Exception:
        return None


__all__ = ["router"]
