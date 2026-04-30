"""Cross-channel analytics endpoints, scoped by organization.

Mounted at ``/api/v1/analytics``. Built incrementally — each endpoint is
independent and follows the same pattern: auth via ``require_org_context``,
``organization_id`` resolved from the auth context (never the request),
service-layer aggregation in Postgres, response shapes defined in
``app/models/analytics.py``.

The wide ClickHouse ``events`` table that the directive proposes is
deferred along with ClickHouse provisioning itself; Postgres serves all
endpoints today and the response payloads carry ``"source": "postgres"``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.models.analytics import (
    CampaignSummaryResponse,
    ReliabilityResponse,
    StepSummaryResponse,
)
from app.services.campaign_analytics import (
    CampaignNotFound,
    summarize_campaign,
)
from app.services.reliability_analytics import summarize_reliability
from app.services.step_analytics import StepNotFound, summarize_step

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


_MAX_WINDOW_DAYS = 93


def _resolve_window(
    start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    end_eff = end or now
    start_eff = start or (end_eff - timedelta(days=30))
    if end_eff <= start_eff:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_window", "message": "end must be after start"},
        )
    if (end_eff - start_eff).days > _MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "window_too_large",
                "message": f"max window is {_MAX_WINDOW_DAYS} days",
            },
        )
    return start_eff, end_eff


@router.get("/reliability", response_model=ReliabilityResponse)
async def reliability(
    user: UserContext = Depends(require_org_context),
    brand_id: str | None = Query(default=None),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> ReliabilityResponse:
    """Webhook ingestion health rolled up by provider.

    Filters to webhooks tagged with brands belonging to the caller's org.
    Pass ``brand_id`` to drill into one brand. ``from``/``to`` default to
    a 30-day trailing window; max window is 93 days (matches OEX).
    """
    assert user.active_organization_id is not None
    start_eff, end_eff = _resolve_window(start, end)

    from uuid import UUID

    brand_uuid: UUID | None = None
    if brand_id is not None:
        try:
            brand_uuid = UUID(brand_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_brand_id"},
            ) from exc

    payload = await summarize_reliability(
        organization_id=user.active_organization_id,
        brand_id=brand_uuid,
        start=start_eff,
        end=end_eff,
    )
    return ReliabilityResponse.model_validate(payload)


@router.get(
    "/campaigns/{campaign_id}/summary",
    response_model=CampaignSummaryResponse,
)
async def campaign_summary(
    campaign_id: str,
    user: UserContext = Depends(require_org_context),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> CampaignSummaryResponse:
    """Per-channel + per-channel_campaign + per-step rollup for a campaign.

    The ``campaign_id`` must belong to the caller's active organization;
    otherwise the endpoint returns 404 (we never leak existence across
    orgs). Window defaults to the trailing 30 days, capped at 93.
    """
    assert user.active_organization_id is not None
    start_eff, end_eff = _resolve_window(start, end)

    from uuid import UUID

    try:
        campaign_uuid = UUID(campaign_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_campaign_id"},
        ) from exc

    try:
        payload = await summarize_campaign(
            organization_id=user.active_organization_id,
            campaign_id=campaign_uuid,
            start=start_eff,
            end=end_eff,
        )
    except CampaignNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "campaign_not_found"},
        ) from exc
    return CampaignSummaryResponse.model_validate(payload)


@router.get(
    "/channel-campaign-steps/{step_id}/summary",
    response_model=StepSummaryResponse,
)
async def channel_campaign_step_summary(
    step_id: str,
    user: UserContext = Depends(require_org_context),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> StepSummaryResponse:
    """Per-step drilldown — membership funnel, event-type breakdown,
    outcomes, and the per-piece status funnel for direct_mail steps.

    The ``step_id`` must belong to the caller's active organization;
    otherwise the endpoint returns 404. Voice/SMS step ids that don't
    resolve to a real ``business.channel_campaign_steps`` row also 404.
    """
    assert user.active_organization_id is not None
    start_eff, end_eff = _resolve_window(start, end)

    from uuid import UUID

    try:
        step_uuid = UUID(step_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_step_id"},
        ) from exc

    try:
        payload = await summarize_step(
            organization_id=user.active_organization_id,
            step_id=step_uuid,
            start=start_eff,
            end=end_eff,
        )
    except StepNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "step_not_found"},
        ) from exc
    return StepSummaryResponse.model_validate(payload)


__all__ = ["router"]
