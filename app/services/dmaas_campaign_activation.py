"""Run the opinionated DMaaS campaign activation pipeline.

Extracted from ``app.routers.dmaas_campaigns`` so the same function can
be called from:

  * the async router (Slice 1) — which schedules a Trigger.dev task that
    calls hq-x's ``/internal/dmaas/process-job`` endpoint, which in turn
    calls this function with the persisted payload.
  * (future) cron-scheduled scheduled-step activations (Slice 4).

The pipeline mirrors what the V1 (synchronous) router did:

  1. create campaigns row
  2. create channel_campaign (direct_mail / lob)
  3. create channel_campaign_step + landing_page_config
  4. materialize recipients
  5. activate the step (Lob adapter mints Dub links + uploads CSV)

Failure mid-flow leaves a partially-created campaign row that the
operator can inspect / clean up. The job's ``error`` JSON carries the
identity of every row already created so the caller can resume.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

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

try:
    from app.services import brand_domains as brand_domains_svc  # type: ignore[import]
except ImportError:  # pragma: no cover — sibling PR; fallback degrades cleanly
    brand_domains_svc = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class DMaaSActivationError(Exception):
    """Raised by the activation pipeline. ``detail`` carries any
    partially-created ids so a job-level error can surface them."""

    def __init__(self, message: str, *, error_code: str, detail: dict[str, Any]):
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail


async def _resolve_landing_page_url(
    *, brand_id: UUID, step_id: UUID
) -> str | None:
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


async def run_campaign_activation(
    *,
    organization_id: UUID,
    user_id: UUID | None,
    name: str,
    brand_id: UUID,
    description: str | None,
    send_date: Any | None,
    creative_payload: dict[str, Any],
    use_landing_page: bool,
    landing_page_config: dict[str, Any] | None,
    destination_url_override: str | None,
    recipients: list[RecipientSpec],
) -> dict[str, Any]:
    """Run the full campaign+activation pipeline.

    Returns a dict shaped like ``DMaaSCampaignCreateResponse``. Raises
    ``DMaaSActivationError`` with a structured detail on failure.
    """
    # Stage 1 — campaign.
    try:
        campaign = await campaigns_svc.create_campaign(
            organization_id=organization_id,
            payload=CampaignCreate(
                brand_id=brand_id,
                name=name,
                description=description,
                start_date=send_date,
            ),
            created_by_user_id=user_id,
        )
    except campaigns_svc.CampaignBrandMismatch as exc:
        raise DMaaSActivationError(
            "brand not found in organization",
            error_code="brand_not_found",
            detail={"brand_id": str(brand_id)},
        ) from exc

    # Stage 2 — channel_campaign.
    cc = await channel_campaigns_svc.create_channel_campaign(
        organization_id=organization_id,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name=name,
            channel="direct_mail",
            provider="lob",
            audience_snapshot_count=len(recipients),
            start_offset_days=0,
        ),
        created_by_user_id=user_id,
    )

    # Stage 3 — step.
    channel_specific: dict[str, Any] = {"lob_creative_payload": creative_payload}
    if use_landing_page:
        channel_specific["destination_url_override"] = None
    else:
        channel_specific["destination_url_override"] = destination_url_override

    step = await steps_svc.create_step(
        channel_campaign_id=cc.id,
        organization_id=organization_id,
        payload=ChannelCampaignStepCreate(
            step_order=1,
            name=name,
            delay_days_from_previous=0,
            creative_ref=None,
            channel_specific_config=channel_specific,
        ),
    )

    landing_url: str | None = None
    if use_landing_page and landing_page_config is not None:
        # Re-validate via the model so we don't persist a stale shape.
        validated = StepLandingPageConfig(**landing_page_config)
        await steps_svc.set_step_landing_page_config(
            step_id=step.id,
            organization_id=organization_id,
            config=validated.model_dump(exclude_none=True),
        )
        landing_url = await _resolve_landing_page_url(
            brand_id=brand_id, step_id=step.id
        )

    partial_ids = {
        "campaign_id": str(campaign.id),
        "channel_campaign_id": str(cc.id),
        "step_id": str(step.id),
    }

    # Stage 4 — audience materialization.
    try:
        await steps_svc.materialize_step_audience(
            step_id=step.id,
            organization_id=organization_id,
            recipients=recipients,
        )
    except steps_svc.StepAudienceImmutable as exc:
        raise DMaaSActivationError(
            str(exc),
            error_code="audience_immutable",
            detail=partial_ids,
        ) from exc

    # Stage 5 — activation.
    try:
        activated = await steps_svc.activate_step(
            step_id=step.id, organization_id=organization_id
        )
    except steps_svc.StepActivationNotImplemented as exc:
        raise DMaaSActivationError(
            str(exc),
            error_code="activation_not_implemented",
            detail=partial_ids,
        ) from exc
    except steps_svc.StepInvalidStatusTransition as exc:
        raise DMaaSActivationError(
            str(exc),
            error_code="invalid_status_transition",
            detail=partial_ids,
        ) from exc

    return {
        "campaign_id": str(campaign.id),
        "channel_campaign_id": str(cc.id),
        "step_id": str(activated.id),
        "external_provider_id": activated.external_provider_id,
        "scheduled_send_at": (
            activated.scheduled_send_at.isoformat()
            if activated.scheduled_send_at is not None
            else None
        ),
        "recipient_count": len(recipients),
        "landing_page_url": landing_url,
        "status": activated.status,
    }


__all__ = [
    "DMaaSActivationError",
    "run_campaign_activation",
]
