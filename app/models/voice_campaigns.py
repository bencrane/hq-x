from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Voice Campaign Config
# ---------------------------------------------------------------------------

AmdStrategy = Literal["vapi", "twilio", "none"]


class VoiceCampaignConfigCreate(BaseModel):
    voice_assistant_id: str
    voice_phone_number_id: str
    voicemail_drop_id: str | None = None
    amd_strategy: AmdStrategy = "vapi"
    max_concurrent_calls: int = 5
    call_window_start: time | None = None
    call_window_end: time | None = None
    call_window_timezone: str = "America/New_York"
    retry_policy: dict[str, Any] = Field(
        default_factory=lambda: {"max_attempts": 3, "delay_hours": 4, "backoff_multiplier": 1.5}
    )

    model_config = {"extra": "forbid"}


class VoiceCampaignConfigResponse(BaseModel):
    id: str
    brand_id: str
    campaign_id: str
    voice_assistant_id: str | None = None
    voice_phone_number_id: str | None = None
    voicemail_drop_id: str | None = None
    amd_strategy: str | None = None
    max_concurrent_calls: int | None = None
    call_window_start: time | None = None
    call_window_end: time | None = None
    call_window_timezone: str | None = None
    retry_policy: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Voice Campaign Batch
# ---------------------------------------------------------------------------


class VoiceCampaignBatchRequest(BaseModel):
    batch_size: int = 10

    model_config = {"extra": "forbid"}


class VoiceCampaignBatchResponse(BaseModel):
    calls_initiated: int
    calls_skipped: int
    reason: str | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Voice Campaign Metrics
# ---------------------------------------------------------------------------


class VoiceCampaignMetricsResponse(BaseModel):
    campaign_id: str
    total_calls: int = 0
    calls_connected: int = 0
    calls_voicemail: int = 0
    calls_no_answer: int = 0
    calls_busy: int = 0
    calls_error: int = 0
    calls_transferred: int = 0
    calls_qualified: int = 0
    total_duration_seconds: int = 0
    total_cost_cents: int = 0
    updated_at: datetime

    model_config = {"extra": "forbid"}
