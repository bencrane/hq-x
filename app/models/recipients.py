"""Pydantic models for the channel-agnostic recipients identity layer.

A recipient is the org-scoped stable identity for a business / property /
person we contact across any channel. Memberships
(``channel_campaign_step_recipients``) link a recipient to a specific
step's audience with a lifecycle status.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

RecipientType = Literal["business", "property", "person", "other"]

StepRecipientStatus = Literal[
    "pending", "scheduled", "sent", "failed", "suppressed", "cancelled"
]


class RecipientSpec(BaseModel):
    """Input shape for upsert. ``external_source`` + ``external_id`` are
    the natural key inside an organization."""

    external_source: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    recipient_type: RecipientType = "business"
    display_name: str | None = None
    mailing_address: dict[str, Any] = Field(default_factory=dict)
    phone: str | None = None
    email: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class RecipientResponse(BaseModel):
    id: UUID
    organization_id: UUID
    recipient_type: RecipientType
    external_source: str
    external_id: str
    display_name: str | None = None
    mailing_address: dict[str, Any]
    phone: str | None = None
    email: str | None = None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


class StepRecipientResponse(BaseModel):
    id: UUID
    channel_campaign_step_id: UUID
    recipient_id: UUID
    organization_id: UUID
    status: StepRecipientStatus
    scheduled_for: datetime | None = None
    processed_at: datetime | None = None
    error_reason: str | None = None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


__all__ = [
    "RecipientType",
    "StepRecipientStatus",
    "RecipientSpec",
    "RecipientResponse",
    "StepRecipientResponse",
]
