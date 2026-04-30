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
from app.models.analytics import ReliabilityResponse
from app.services.reliability_analytics import summarize_reliability

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


__all__ = ["router"]
