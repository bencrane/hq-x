"""Pydantic models for gtm_motions and channel-typed campaigns."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── Channel + provider taxonomy ────────────────────────────────────────────
#
# Channel values are kept narrow on purpose; only the four below ship today.
# Provider is the concrete external service. The tuple (channel, provider)
# constrains the surface area: e.g. channel='email' currently only accepts
# provider='emailbison' even though the column-level CHECK is permissive.

Channel = Literal["direct_mail", "email", "voice_outbound", "sms"]
Provider = Literal["lob", "emailbison", "twilio", "vapi", "manual"]

VALID_CHANNEL_PROVIDER_PAIRS: set[tuple[str, str]] = {
    ("direct_mail", "lob"),
    ("direct_mail", "manual"),
    ("email", "emailbison"),
    ("email", "manual"),
    ("voice_outbound", "vapi"),
    ("voice_outbound", "twilio"),
    ("sms", "twilio"),
}


MotionStatus = Literal["draft", "active", "paused", "completed", "archived"]
CampaignStatus = Literal[
    "draft", "scheduled", "sending", "sent", "paused", "failed", "archived"
]


# ── GTM motions ────────────────────────────────────────────────────────────


class GtmMotionCreate(BaseModel):
    brand_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    start_date: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class GtmMotionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: MotionStatus | None = None
    start_date: date | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class GtmMotionResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID
    name: str
    description: str | None = None
    status: MotionStatus
    start_date: date | None = None
    metadata: dict[str, Any]
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    model_config = {"extra": "forbid"}


# ── Campaigns ──────────────────────────────────────────────────────────────


class CampaignCreate(BaseModel):
    gtm_motion_id: UUID
    name: str = Field(min_length=1, max_length=200)
    channel: Channel
    provider: Provider
    audience_spec_id: UUID | None = None
    audience_snapshot_count: int | None = Field(default=None, ge=0)
    start_offset_days: int = Field(default=0, ge=0)
    schedule_config: dict[str, Any] = Field(default_factory=dict)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    design_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    audience_spec_id: UUID | None = None
    audience_snapshot_count: int | None = Field(default=None, ge=0)
    start_offset_days: int | None = Field(default=None, ge=0)
    schedule_config: dict[str, Any] | None = None
    provider_config: dict[str, Any] | None = None
    design_id: UUID | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class CampaignResponse(BaseModel):
    id: UUID
    gtm_motion_id: UUID
    organization_id: UUID
    brand_id: UUID
    name: str
    channel: Channel
    provider: Provider
    audience_spec_id: UUID | None = None
    audience_snapshot_count: int | None = None
    status: CampaignStatus
    start_offset_days: int
    scheduled_send_at: datetime | None = None
    schedule_config: dict[str, Any]
    provider_config: dict[str, Any]
    design_id: UUID | None = None
    metadata: dict[str, Any]
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    model_config = {"extra": "forbid"}


__all__ = [
    "VALID_CHANNEL_PROVIDER_PAIRS",
    "Channel",
    "Provider",
    "MotionStatus",
    "CampaignStatus",
    "GtmMotionCreate",
    "GtmMotionUpdate",
    "GtmMotionResponse",
    "CampaignCreate",
    "CampaignUpdate",
    "CampaignResponse",
]
