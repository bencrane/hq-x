"""Pydantic response models for the analytics router.

Built incrementally alongside the analytics endpoints. Each endpoint
gets its own response shape so consumers can rely on stable field names
even as the underlying queries evolve.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class _Window(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


# ── Reliability ─────────────────────────────────────────────────────────


class ReliabilityProvider(BaseModel):
    provider_slug: str
    events_total: int
    replays_total: int
    by_status: dict[str, int]


class ReliabilityTotals(BaseModel):
    events: int
    replays: int


class ReliabilityResponse(BaseModel):
    window: _Window
    providers: list[ReliabilityProvider]
    totals: ReliabilityTotals
    source: Literal["postgres"]


# ── Campaign rollup ─────────────────────────────────────────────────────


class CampaignSummaryCampaign(BaseModel):
    id: str
    organization_id: str
    brand_id: str
    name: str
    status: str
    start_date: str | None = None
    created_at: str | None = None


class CampaignSummaryTotals(BaseModel):
    events_total: int
    unique_recipients_total: int
    cost_total_cents: int


class StepSummary(BaseModel):
    channel_campaign_step_id: str | None
    channel_campaign_id: str | None = None
    step_order: int
    name: str | None = None
    external_provider_id: str | None = None
    events_total: int
    cost_total_cents: int
    outcomes: dict[str, int]
    memberships: dict[str, int]
    synthetic: bool = False
    channel: str | None = None


class ChannelCampaignSummary(BaseModel):
    channel_campaign_id: str
    name: str | None = None
    channel: str
    provider: str
    status: str
    scheduled_send_at: str | None = None
    events_total: int
    unique_recipients: int
    cost_total_cents: int
    outcomes: dict[str, int]
    steps: list[StepSummary]
    voice_step_attribution: Literal["synthetic"] | None = None
    sms_step_attribution: Literal["synthetic"] | None = None


class ChannelRollup(BaseModel):
    channel: str
    events_total: int
    unique_recipients: int
    outcomes: dict[str, int]
    cost_total_cents: int


class ProviderRollup(BaseModel):
    provider: str
    events_total: int
    outcomes: dict[str, int]
    cost_total_cents: int


class CampaignSummaryResponse(BaseModel):
    campaign: CampaignSummaryCampaign
    window: _Window
    totals: CampaignSummaryTotals
    channel_campaigns: list[ChannelCampaignSummary]
    by_channel: list[ChannelRollup]
    by_provider: list[ProviderRollup]
    source: Literal["postgres"]


# ── Step funnel ─────────────────────────────────────────────────────────


class StepSummaryStep(BaseModel):
    id: str
    channel_campaign_id: str
    campaign_id: str
    step_order: int
    name: str | None = None
    channel: str
    provider: str
    external_provider_id: str | None = None
    status: str
    scheduled_send_at: str | None = None
    activated_at: str | None = None


class StepEventsBlock(BaseModel):
    total: int
    by_event_type: dict[str, int]
    outcomes: dict[str, int]
    cost_total_cents: int


class StepSummaryResponse(BaseModel):
    step: StepSummaryStep
    window: _Window
    events: StepEventsBlock
    memberships: dict[str, int]
    channel_specific: dict[str, dict[str, dict[str, int]]]
    source: Literal["postgres"]


# ── Recipient timeline ──────────────────────────────────────────────────


class RecipientTimelineRecipient(BaseModel):
    id: str
    organization_id: str
    recipient_type: str
    external_source: str
    external_id: str
    display_name: str | None = None
    created_at: str | None = None


class RecipientTimelineSummary(BaseModel):
    total_events: int
    by_channel: dict[str, int]
    campaigns_touched: int
    channel_campaigns_touched: int


class RecipientTimelineEvent(BaseModel):
    occurred_at: str | None
    channel: str
    provider: str
    event_type: str
    campaign_id: str | None = None
    channel_campaign_id: str | None = None
    channel_campaign_step_id: str | None = None
    artifact_id: str | None = None
    artifact_kind: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecipientTimelinePagination(BaseModel):
    limit: int
    offset: int
    total: int


class RecipientTimelineResponse(BaseModel):
    recipient: RecipientTimelineRecipient
    window: _Window
    summary: RecipientTimelineSummary
    events: list[RecipientTimelineEvent]
    pagination: RecipientTimelinePagination
    source: Literal["postgres"]


# ── Direct-mail funnel ──────────────────────────────────────────────────


class DirectMailTotals(BaseModel):
    pieces: int
    delivered: int
    in_transit: int
    returned: int
    failed: int
    test_mode_count: int


class DirectMailByPieceTypeItem(BaseModel):
    piece_type: str
    count: int
    delivered: int
    failed: int


class DirectMailDailyTrendItem(BaseModel):
    date: str
    created: int
    delivered: int
    failed: int


class DirectMailFailureReasonItem(BaseModel):
    reason: str
    count: int


class DirectMailAnalyticsResponse(BaseModel):
    window: _Window
    totals: DirectMailTotals
    funnel: dict[str, int]
    by_piece_type: list[DirectMailByPieceTypeItem]
    daily_trends: list[DirectMailDailyTrendItem]
    failure_reason_breakdown: list[DirectMailFailureReasonItem]
    source: Literal["postgres"]


__all__ = [
    "CampaignSummaryCampaign",
    "CampaignSummaryResponse",
    "CampaignSummaryTotals",
    "ChannelCampaignSummary",
    "ChannelRollup",
    "DirectMailAnalyticsResponse",
    "DirectMailByPieceTypeItem",
    "DirectMailDailyTrendItem",
    "DirectMailFailureReasonItem",
    "DirectMailTotals",
    "ProviderRollup",
    "RecipientTimelineEvent",
    "RecipientTimelinePagination",
    "RecipientTimelineRecipient",
    "RecipientTimelineResponse",
    "RecipientTimelineSummary",
    "ReliabilityProvider",
    "ReliabilityResponse",
    "ReliabilityTotals",
    "StepEventsBlock",
    "StepSummary",
    "StepSummaryResponse",
    "StepSummaryStep",
]
