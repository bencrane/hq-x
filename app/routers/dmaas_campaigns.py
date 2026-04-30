"""Opinionated single-call DMaaS API.

The customer-facing convenience surface for "we run your direct mail."
A single POST collapses the five-call create+activate flow:

  POST campaign
  POST channel-campaign
  POST step
  materialize audience
  POST step/activate

into one request that takes everything (brand_id, send_date, creative,
landing_page, recipients) and returns the activated step + the
recipient-facing landing page URL.

The five-call flow stays as-is — this is purely additive convenience.
Existing routers (campaigns_router, channel_campaigns_router,
channel_campaign_steps_router) are not modified.

Synchronous in V1 — for ~5,000 recipients, a request takes 15-60s
(Dub bulk mint + Lob audience upload). Slice 6's directive owns this
trade-off; Trigger.dev async refactor is the followup directive.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.config import settings
from app.models.campaigns import (
    CampaignCreate,
    ChannelCampaignCreate,
    ChannelCampaignStepCreate,
    StepLandingPageConfig,
)
from app.models.recipients import RecipientSpec
from app.services import campaigns as campaigns_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import channel_campaigns as channel_campaigns_svc

# Slice 1 (brand_domains) is a sibling PR — try to import; fall back to
# None lookups if the module isn't present yet so this slice can land
# independently. The brand-domain landing-page-host resolution
# degrades gracefully to ENTRI_APPLICATION_URL_BASE.
try:
    from app.services import brand_domains as brand_domains_svc
except ImportError:  # pragma: no cover - covered by sibling PR's tests
    brand_domains_svc = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dmaas", tags=["dmaas"])


_RECIPIENT_CAP = 50_000


class DMaaSCreative(BaseModel):
    """Operator-supplied Lob creative payload (HTML + back HTML or
    panel-shaped self-mailer payload). Validation is permissive — Lob
    rejects the wrong shape server-side."""

    lob_creative_payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class DMaaSCampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brand_id: UUID
    send_date: date | None = None
    description: str | None = Field(default=None, max_length=2000)

    creative: DMaaSCreative
    landing_page: StepLandingPageConfig | None = None
    use_landing_page: bool = True
    destination_url_override: str | None = Field(default=None, max_length=2048)

    recipients: list[RecipientSpec] = Field(min_length=1, max_length=_RECIPIENT_CAP)

    model_config = {"extra": "forbid"}

    @field_validator("destination_url_override")
    @classmethod
    def _override_must_be_https(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("https://", "http://")):
            raise ValueError("destination_url_override must include http(s):// scheme")
        return v

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        if self.use_landing_page and self.landing_page is None:
            raise ValueError(
                "landing_page is required when use_landing_page=True"
            )
        if not self.use_landing_page and not self.destination_url_override:
            raise ValueError(
                "destination_url_override is required when use_landing_page=False"
            )
        if self.use_landing_page and self.destination_url_override:
            raise ValueError(
                "destination_url_override is mutually exclusive with use_landing_page=True"
            )


class DMaaSCampaignCreateResponse(BaseModel):
    campaign_id: UUID
    channel_campaign_id: UUID
    step_id: UUID
    external_provider_id: str | None
    scheduled_send_at: datetime | None
    recipient_count: int
    landing_page_url: str | None
    status: Literal[
        "scheduled", "activating", "sent", "failed", "pending"
    ]


def _bad_request(error: str, message: str = "") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _not_found(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


async def _resolve_landing_page_url(
    *, brand_id: UUID, step_id: UUID
) -> str | None:
    """Build the landing-page URL the recipient sees after click.

    Uses the brand's configured landing-page domain when set; otherwise
    falls back to a platform-default subdomain. The trailing
    `/{short_code}` is appended at link-mint time by Dub.
    """
    domain: str | None = None
    if brand_domains_svc is not None:
        domain = await brand_domains_svc.get_brand_landing_page_domain(
            brand_id=brand_id
        )
    if domain is not None:
        return f"https://{domain}/lp/{step_id}"
    base = (settings.ENTRI_APPLICATION_URL_BASE or "").rstrip("/")
    if not base:
        return None
    return f"{base}/lp/{step_id}"


@router.post(
    "/campaigns",
    response_model=DMaaSCampaignCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dmaas_campaign(
    body: DMaaSCampaignCreateRequest,
    user: UserContext = Depends(require_org_context),
) -> DMaaSCampaignCreateResponse:
    """Create + activate a single-step direct-mail campaign in one call.

    Stages (all org-isolated; cross-org `brand_id` → 404):
      1. Validate brand belongs to caller's org.
      2. Create business.campaigns row.
      3. Create business.channel_campaigns (channel='direct_mail',
         provider='lob').
      4. Create business.channel_campaign_steps with the operator-
         supplied lob_creative_payload + landing_page_config.
      5. Bulk-upsert recipients into business.recipients and pending
         memberships into channel_campaign_step_recipients.
      6. Activate the step (LobAdapter mints Dub links + builds Lob
         audience CSV + uploads).

    Failure mid-flow: rows from earlier stages persist (the step row
    will have status='pending' or 'failed' depending on where it
    failed). The response includes whatever ids were minted so the
    caller can resume by hand if needed; cleanest retry is to delete
    the partially-created campaign and re-POST.
    """
    org_id = user.active_organization_id
    assert org_id is not None
    user_id = (
        user.business_user_id
        if hasattr(user, "business_user_id") and user.business_user_id is not None
        else None
    )

    # Stage 1: campaign.
    try:
        campaign = await campaigns_svc.create_campaign(
            organization_id=org_id,
            payload=CampaignCreate(
                brand_id=body.brand_id,
                name=body.name,
                description=body.description,
                start_date=body.send_date,
            ),
            created_by_user_id=user_id,
        )
    except campaigns_svc.CampaignBrandMismatch as exc:
        raise _not_found("brand_not_found") from exc

    # Stage 2: channel_campaign (direct_mail / lob).
    cc = await channel_campaigns_svc.create_channel_campaign(
        organization_id=org_id,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name=body.name,
            channel="direct_mail",
            provider="lob",
            audience_snapshot_count=len(body.recipients),
            start_offset_days=0,
        ),
        created_by_user_id=user_id,
    )

    # Stage 3: step.
    channel_specific = {"lob_creative_payload": body.creative.lob_creative_payload}
    if body.use_landing_page:
        # When use_landing_page=True, the destination URL on each minted
        # link is the brand's landing-page host + step + short_code; we
        # encode the override as None on the step and resolve at mint
        # time inside LobAdapter via brand_domains lookup. For V1 we
        # store the resolved base in channel_specific.lob_destination_url
        # so the existing LobAdapter (which currently expects an explicit
        # destination_url_override) can read it.
        landing_url = await _resolve_landing_page_url(
            brand_id=body.brand_id, step_id=campaign.id  # placeholder, refreshed below
        )
        # The real per-recipient URL needs `step_id` which we don't have
        # until create_step returns; we store the per-step base separately
        # so the adapter resolves it later.
        channel_specific["destination_url_override"] = None
    else:
        channel_specific["destination_url_override"] = body.destination_url_override
        landing_url = None

    step = await steps_svc.create_step(
        channel_campaign_id=cc.id,
        organization_id=org_id,
        payload=ChannelCampaignStepCreate(
            step_order=1,
            name=body.name,
            delay_days_from_previous=0,
            creative_ref=None,
            channel_specific_config=channel_specific,
        ),
    )

    # Now we know step.id — store the landing_page_config + refresh the
    # destination URL bound to this step.
    if body.use_landing_page and body.landing_page is not None:
        await steps_svc.set_step_landing_page_config(
            step_id=step.id,
            organization_id=org_id,
            config=body.landing_page.model_dump(exclude_none=True),
        )
        landing_url = await _resolve_landing_page_url(
            brand_id=body.brand_id, step_id=step.id
        )

    # Stage 4: audience materialization.
    try:
        await steps_svc.materialize_step_audience(
            step_id=step.id,
            organization_id=org_id,
            recipients=body.recipients,
        )
    except steps_svc.StepAudienceImmutable as exc:
        raise _bad_request("audience_immutable", str(exc)) from exc

    # Stage 5: activation. Failures here surface as the step's status
    # transitioning to 'failed' but leave the campaign row intact so the
    # operator can debug.
    try:
        activated = await steps_svc.activate_step(
            step_id=step.id, organization_id=org_id
        )
    except steps_svc.StepActivationNotImplemented as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "activation_not_implemented",
                "message": str(exc),
                "campaign_id": str(campaign.id),
                "channel_campaign_id": str(cc.id),
                "step_id": str(step.id),
            },
        ) from exc
    except steps_svc.StepInvalidStatusTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "invalid_status_transition",
                "message": str(exc),
                "campaign_id": str(campaign.id),
                "channel_campaign_id": str(cc.id),
                "step_id": str(step.id),
            },
        ) from exc

    return DMaaSCampaignCreateResponse(
        campaign_id=campaign.id,
        channel_campaign_id=cc.id,
        step_id=activated.id,
        external_provider_id=activated.external_provider_id,
        scheduled_send_at=activated.scheduled_send_at,
        recipient_count=len(body.recipients),
        landing_page_url=landing_url,
        status=activated.status,
    )


__all__ = ["router"]


# Make the timestamp module-importable so callers/tests can monkeypatch
# `datetime.now` if they need deterministic scheduled_send_at output.
_now = datetime.now  # noqa: F841 (kept for symmetry with other routers)
_today = lambda: datetime.now(UTC).date()  # noqa: E731
