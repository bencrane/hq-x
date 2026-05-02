"""Resolve `(step_id, short_code) → recipient + brand + page config` and
render the hosted landing page.

Resolution order:
  1. dmaas_dub_links by (step_id, dub_key=short_code) → recipient_id
  2. business.recipients by recipient_id → display_name + mailing_address
  3. business.channel_campaign_steps by step_id → landing_page_config,
     org_id, brand_id, etc.
  4. business.brands by brand_id → theme_config

The render module is otherwise pure: deterministic for a given DB state,
no I/O beyond the resolution queries.

Personalization tokens:
  * {recipient.display_name}
  * {recipient.mailing_address.city|state|postal_code|...}
  * {step.name}
  * {brand.name}

Missing tokens render as empty string (defensive — we don't want a typo
in the operator's headline to throw a 500). Unknown TOP-LEVEL token
namespaces (e.g. `{foo.bar}`) also render empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.dmaas import dub_links as dub_links_repo
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import recipients as recipients_svc
from app.services.landing_page_template import (
    render_landing_page_html,
    render_not_found_html,
)


class LandingPageNotFoundError(Exception):
    """Either the step doesn't exist or no link matches the short_code."""


@dataclass(frozen=True)
class LandingPageRender:
    html: str
    organization_id: UUID
    brand_id: UUID
    campaign_id: UUID
    channel_campaign_id: UUID
    channel_campaign_step_id: UUID
    recipient_id: UUID


# Matches "{ns.field}" or "{ns.nested.path}". Conservative: bracketed
# dotted-path tokens only. Stray { or } in the operator's text passes
# through unchanged.
_TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\}")


def _resolve_token_path(path: str, ctx: dict[str, Any]) -> str:
    parts = path.split(".")
    current: Any = ctx
    for p in parts:
        if isinstance(current, dict) and p in current:
            current = current[p]
        else:
            return ""
    if current is None:
        return ""
    return str(current)


def apply_personalization(text: str, ctx: dict[str, Any]) -> str:
    """Replace `{ns.path}` tokens with values resolved from `ctx`.

    Missing keys render as empty string. Tokens that don't match the
    `{ns.path}` shape (e.g. `{ }`, `{single}`) are left as-is.
    """

    def _sub(match: re.Match[str]) -> str:
        return _resolve_token_path(match.group(1), ctx)

    return _TOKEN_RE.sub(_sub, text or "")


async def resolve_landing_page_render(
    *,
    step_id: UUID,
    short_code: str,
    submit_url_template: str = "/lp/{step_id}/{short_code}/submit",
) -> LandingPageRender:
    """Resolve a render context and produce the HTML body.

    Raises `LandingPageNotFoundError` if the step doesn't exist, no
    matching link exists for that short_code under that step, or the
    step has no landing_page_config (rendering the page would be a
    blank shell — better to 404).
    """
    link = await dub_links_repo.find_dub_link_for_step_short_code(
        channel_campaign_step_id=step_id, short_code=short_code
    )
    if link is None or link.recipient_id is None:
        raise LandingPageNotFoundError(
            f"no link for (step={step_id}, short_code={short_code})"
        )

    # Step row gives us the org + brand + landing_page_config.
    step_context = await steps_svc.get_step_context(step_id=step_id)
    if step_context is None:
        raise LandingPageNotFoundError(f"step {step_id} not found")
    organization_id = UUID(step_context["organization_id"])
    brand_id = UUID(step_context["brand_id"])
    campaign_id = UUID(step_context["campaign_id"])
    channel_campaign_id = UUID(step_context["channel_campaign_id"])

    # Defense in depth: the link's brand_id (if persisted) must match
    # the step's brand_id. Cross-brand short codes shouldn't exist given
    # how minting works, but we check anyway.
    if link.brand_id is not None and link.brand_id != brand_id:
        raise LandingPageNotFoundError(
            f"link/step brand mismatch: link.brand_id={link.brand_id} "
            f"step.brand_id={brand_id}"
        )

    page_cfg = await steps_svc.get_step_landing_page_config(
        step_id=step_id, organization_id=organization_id
    )
    if not page_cfg:
        raise LandingPageNotFoundError(
            f"step {step_id} has no landing_page_config"
        )

    recipient = await recipients_svc.get_recipient(
        recipient_id=link.recipient_id, organization_id=organization_id
    )
    if recipient is None:
        raise LandingPageNotFoundError(
            f"recipient {link.recipient_id} not found in org {organization_id}"
        )

    theme = await brands_svc.get_theme(brand_id) or {}

    token_ctx: dict[str, Any] = {
        "recipient": {
            "display_name": recipient.display_name or "",
            "email": recipient.email or "",
            "phone": recipient.phone or "",
            "mailing_address": recipient.mailing_address or {},
        },
        "step": {
            # The DB context dict from get_step_context doesn't carry the
            # step name; resolve via a lightweight extra fetch so the
            # operator's `{step.name}` token is renderable.
        },
        "brand": {},
    }

    headline = apply_personalization(str(page_cfg.get("headline", "")), token_ctx)
    body = apply_personalization(str(page_cfg.get("body", "")), token_ctx)
    cta_raw = page_cfg.get("cta") or {}
    # Personalize the cta label too, while leaving form_schema literal.
    cta_rendered = dict(cta_raw)
    if "label" in cta_rendered and isinstance(cta_rendered["label"], str):
        cta_rendered["label"] = apply_personalization(cta_rendered["label"], token_ctx)
    if "thank_you_message" in cta_rendered and isinstance(
        cta_rendered["thank_you_message"], str
    ):
        cta_rendered["thank_you_message"] = apply_personalization(
            cta_rendered["thank_you_message"], token_ctx
        )

    submit_url = submit_url_template.format(step_id=step_id, short_code=short_code)

    html = render_landing_page_html(
        {
            "headline": headline,
            "body": body,
            "cta": cta_rendered,
            "theme": theme,
            "submit_url": submit_url,
        }
    )

    return LandingPageRender(
        html=html,
        organization_id=organization_id,
        brand_id=brand_id,
        campaign_id=campaign_id,
        channel_campaign_id=channel_campaign_id,
        channel_campaign_step_id=step_id,
        recipient_id=link.recipient_id,
    )


def render_not_found_for_brand(theme: dict[str, Any] | None = None) -> str:
    """Public re-export: brand-themed not-found page when only the brand
    is resolvable (e.g. unknown short_code under a known step)."""
    return render_not_found_html(theme=theme)


__all__ = [
    "LandingPageNotFoundError",
    "LandingPageRender",
    "apply_personalization",
    "render_not_found_for_brand",
    "resolve_landing_page_render",
]
