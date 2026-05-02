"""Pydantic models for hosted-landing-page form submissions.

The submit endpoint accepts form_data verbatim from the rendered HTML
form; validation against the step's form_schema lives in the service
layer (it depends on per-step config, not a shared static schema).

These models cover the API-side response shape — what dashboard
endpoints serve back to the customer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class LandingPageSubmissionResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID
    campaign_id: UUID
    channel_campaign_id: UUID
    channel_campaign_step_id: UUID
    recipient_id: UUID
    form_data: dict[str, Any]
    source_metadata: dict[str, Any] | None = None
    submitted_at: datetime

    model_config = {"extra": "forbid"}


class LandingPageSubmissionsListResponse(BaseModel):
    submissions: list[LandingPageSubmissionResponse]
    total: int


__all__ = [
    "LandingPageSubmissionResponse",
    "LandingPageSubmissionsListResponse",
]
