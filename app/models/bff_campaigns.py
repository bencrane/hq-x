"""Pydantic models for the hq-zone BFF campaign-enrollment surface.

One atomic request shape: take a list of lead rows + a campaign config, and
land a complete campaign + channel_campaign + first step + recipient rows +
step memberships under the supplied (organization_id, brand_id) in a single
transaction. The BFF makes one call instead of five.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models.campaigns import (
    Channel,
    ChannelCampaignStepContentMode,
    Provider,
)
from app.models.recipients import RecipientType


class BffEnrollListRecipient(BaseModel):
    """One lead row from the BFF. ``external_source`` + ``external_id`` are
    the per-org natural key used by upsert.
    """

    external_source: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    recipient_type: RecipientType = "person"
    display_name: str | None = Field(default=None, max_length=256)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    mailing_address: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class BffEnrollListRequest(BaseModel):
    organization_id: UUID
    brand_id: UUID
    campaign_name: str = Field(min_length=1, max_length=200)
    channel: Channel = "email"
    provider: Provider = "emailbison"
    step_name: str | None = Field(default=None, max_length=200)
    content_mode: ChannelCampaignStepContentMode = "manual"
    channel_specific_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    recipients: list[BffEnrollListRecipient] = Field(min_length=1, max_length=10_000)
    source_label: str | None = Field(default=None, max_length=200)

    model_config = {"extra": "forbid"}


class BffEnrollListResponse(BaseModel):
    campaign_id: UUID
    channel_campaign_id: UUID
    step_id: UUID
    recipient_count: int
    recipients_new: int
    recipients_existing: int
    memberships_created: int

    model_config = {"extra": "forbid"}


__all__ = [
    "BffEnrollListRecipient",
    "BffEnrollListRequest",
    "BffEnrollListResponse",
]
