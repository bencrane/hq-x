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
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.dmaas import dub_links as dub_links_repo
from app.dmaas import landing_page_views
from app.observability import incr_metric
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import landing_page_submissions as submissions_svc
from app.services.analytics import emit_event
from app.services.landing_page_render import (
    LandingPageNotFoundError,
    render_not_found_for_brand,
    resolve_landing_page_render,
)
from app.services.landing_page_template import render_thank_you_html

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


# In-process rate limiter for form submissions. (ip_hash, step_id) →
# epoch_seconds-of-last-submission. Sufficient for V1 single-process
# deployment; a distributed-rate-limit primitive (Redis bucket, Upstash)
# is a future PR if hq-x ever scales horizontally.
_SUBMISSION_LAST_SEEN: dict[tuple[str, str], float] = {}
_SUBMISSION_RATE_LIMIT_SECONDS = 30


def _rate_limited(*, ip_hash: str, step_id: UUID) -> bool:
    key = (ip_hash, str(step_id))
    now = time.monotonic()
    last = _SUBMISSION_LAST_SEEN.get(key)
    if last is not None and (now - last) < _SUBMISSION_RATE_LIMIT_SECONDS:
        return True
    _SUBMISSION_LAST_SEEN[key] = now
    return False


@router.post("/lp/{step_id}/{short_code}/submit")
async def submit_landing_page_form(
    step_id: UUID,
    short_code: str,
    request: Request,
) -> Any:
    """Validate, persist, and emit a landing-page form submission.

    Honeypot trip → silent 200 (no signal to bots). Validation failure
    → 422 with per-field error map. Rate-limit trip → 429. Otherwise
    persists the row, fires `page.submitted` (no PII in event
    properties — just the field NAMES), tries Dub `track_lead` if a
    Dub click context is recoverable, and renders the thank-you page
    or redirects to thank_you_redirect_url when supplied.
    """
    # Form bodies arrive as application/x-www-form-urlencoded from the
    # template, but we accept JSON too in case a customer's frontend
    # talks to /submit directly.
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            raw = await request.json()
        except Exception:
            raw = {}
    else:
        form = await request.form()
        raw = {k: v for k, v in form.items()}

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "form_body_invalid", "message": "expected dict-shaped body"},
        )

    # Honeypot: silent 200 with empty thank-you so the bot doesn't learn it failed.
    if str(raw.get("company_website", "")).strip():
        incr_metric("landing_page.honeypot_trip")
        return JSONResponse({"ok": True}, status_code=200)
    raw.pop("company_website", None)

    link = await dub_links_repo.find_dub_link_for_step_short_code(
        channel_campaign_step_id=step_id, short_code=short_code
    )
    if link is None or link.recipient_id is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "landing_page_link_not_found"},
        )

    step_context = await steps_svc.get_step_context(step_id=step_id)
    if step_context is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "step_not_found"},
        )
    organization_id = UUID(step_context["organization_id"])
    brand_id = UUID(step_context["brand_id"])
    campaign_id = UUID(step_context["campaign_id"])
    channel_campaign_id = UUID(step_context["channel_campaign_id"])

    page_cfg = await steps_svc.get_step_landing_page_config(
        step_id=step_id, organization_id=organization_id
    )
    if not page_cfg:
        raise HTTPException(
            status_code=404,
            detail={"error": "landing_page_config_not_found"},
        )
    cta = page_cfg.get("cta") or {}
    if cta.get("type") != "form":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "submit_not_supported",
                "message": "this step's CTA is not a form",
            },
        )
    form_schema = cta.get("form_schema") or {}

    ip_hash = _hash_ip(_client_ip(request))
    if _rate_limited(ip_hash=ip_hash, step_id=step_id):
        incr_metric("landing_page.submit_rate_limited")
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "retry_after_seconds": _SUBMISSION_RATE_LIMIT_SECONDS},
        )

    try:
        clean, extras = submissions_svc.validate_against_schema(
            form_data=raw, form_schema=form_schema
        )
    except submissions_svc.FormValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "form_validation_failed", "errors": exc.errors},
        ) from exc

    persisted = clean if not extras else {**clean, "_extras": extras}
    submission = await submissions_svc.record_submission(
        organization_id=organization_id,
        brand_id=brand_id,
        campaign_id=campaign_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=step_id,
        recipient_id=link.recipient_id,
        form_data=persisted,
        source_metadata=_source_metadata(request, ip_hash=ip_hash),
    )

    # `page.submitted` event — carries field NAMES so RudderStack-side
    # routing can branch, but never the values (PII stays in our DB).
    try:
        await emit_event(
            event_name="page.submitted",
            channel_campaign_step_id=step_id,
            recipient_id=link.recipient_id,
            properties={
                "submission_id": str(submission.id),
                "form_field_names": sorted(clean.keys()),
                "ip_hash": ip_hash,
                "user_agent": (request.headers.get("user-agent") or "")[:500],
                "referrer": (request.headers.get("referer") or "")[:500],
            },
        )
    except Exception:
        logger.exception(
            "landing_page.submit_emit_failed step=%s submission=%s",
            step_id,
            submission.id,
        )

    incr_metric("landing_page.submit_persisted")

    # Thank-you redirect, otherwise themed thank-you page.
    redirect_url = cta.get("thank_you_redirect_url")
    if isinstance(redirect_url, str) and redirect_url:
        return RedirectResponse(redirect_url, status_code=303)
    theme = await brands_svc.get_theme(brand_id) or {}
    msg = cta.get("thank_you_message") or "Thanks — we'll be in touch."
    return HTMLResponse(
        content=render_thank_you_html(message=msg, theme=theme),
        status_code=200,
    )


__all__ = ["router"]
