from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Voice Assistants
# ---------------------------------------------------------------------------

AssistantType = Literal["outbound_qualifier", "inbound_ivr", "callback"]
AssistantStatus = Literal["draft", "active", "archived"]


class VoiceAssistantCreateRequest(BaseModel):
    name: str
    assistant_type: AssistantType
    system_prompt: str | None = None
    first_message: str | None = None
    first_message_mode: str = "assistant-speaks-first"
    model_config_data: dict[str, Any] | None = Field(default=None, alias="model_config")
    voice_config: dict[str, Any] | None = None
    transcriber_config: dict[str, Any] | None = None
    tools_config: list[dict[str, Any]] | None = None
    analysis_config: dict[str, Any] | None = None
    max_duration_seconds: int = 600
    metadata: dict[str, Any] | None = None
    partner_id: str | None = None

    model_config = {"extra": "forbid", "populate_by_name": True}


class VoiceAssistantUpdateRequest(BaseModel):
    name: str | None = None
    assistant_type: AssistantType | None = None
    system_prompt: str | None = None
    first_message: str | None = None
    first_message_mode: str | None = None
    model_config_data: dict[str, Any] | None = Field(default=None, alias="model_config")
    voice_config: dict[str, Any] | None = None
    transcriber_config: dict[str, Any] | None = None
    tools_config: list[dict[str, Any]] | None = None
    analysis_config: dict[str, Any] | None = None
    max_duration_seconds: int | None = None
    metadata: dict[str, Any] | None = None
    status: AssistantStatus | None = None

    model_config = {"extra": "forbid", "populate_by_name": True}


class VoiceAssistantResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str | None = None
    name: str
    assistant_type: str
    vapi_assistant_id: str | None = None
    system_prompt: str | None = None
    first_message: str | None = None
    first_message_mode: str | None = None
    model_config_data: dict[str, Any] | None = Field(default=None, alias="model_config")
    voice_config: dict[str, Any] | None = None
    transcriber_config: dict[str, Any] | None = None
    tools_config: list[dict[str, Any]] | None = None
    analysis_config: dict[str, Any] | None = None
    max_duration_seconds: int | None = None
    metadata: dict[str, Any] | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid", "populate_by_name": True}


# ---------------------------------------------------------------------------
# Voice Phone Numbers
# ---------------------------------------------------------------------------

PhoneNumberPurpose = Literal["outbound", "inbound", "both"]
PhoneNumberStatus = Literal["pending", "active", "inactive", "failed"]


class VoicePhoneNumberCreateRequest(BaseModel):
    phone_number: str
    phone_number_id: str | None = None
    twilio_phone_number_sid: str | None = None
    voice_assistant_id: str | None = None
    label: str | None = None
    purpose: PhoneNumberPurpose
    partner_id: str | None = None

    model_config = {"extra": "forbid"}


class VoicePhoneNumberUpdateRequest(BaseModel):
    voice_assistant_id: str | None = None
    label: str | None = None
    purpose: PhoneNumberPurpose | None = None
    status: PhoneNumberStatus | None = None

    model_config = {"extra": "forbid"}


class VoicePhoneNumberResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str | None = None
    phone_number: str
    phone_number_id: str | None = None
    vapi_phone_number_id: str | None = None
    twilio_phone_number_sid: str | None = None
    provider: str | None = None
    voice_assistant_id: str | None = None
    label: str | None = None
    purpose: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Voicemail Drops
# ---------------------------------------------------------------------------

GenerationStatus = Literal["pending", "generating", "completed", "failed"]


class VoicemailDropCreateRequest(BaseModel):
    name: str
    script: str
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    partner_id: str | None = None

    model_config = {"extra": "forbid"}


class VoicemailDropResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str | None = None
    name: str
    script: str
    voice_id: str
    model_id: str | None = None
    audio_url: str | None = None
    storage_path: str | None = None
    duration_seconds: float | None = None
    generation_status: str | None = None
    generation_error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Transfer Territories
# ---------------------------------------------------------------------------


class TransferTerritoryCreateRequest(BaseModel):
    name: str
    campaign_id: str | None = None
    rules: dict[str, Any] = Field(default_factory=dict)
    destination_phone: str
    destination_label: str | None = None
    priority: int = 0
    partner_id: str | None = None

    model_config = {"extra": "forbid"}


class TransferTerritoryUpdateRequest(BaseModel):
    name: str | None = None
    campaign_id: str | None = None
    rules: dict[str, Any] | None = None
    destination_phone: str | None = None
    destination_label: str | None = None
    priority: int | None = None
    active: bool | None = None

    model_config = {"extra": "forbid"}


class TransferTerritoryResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str | None = None
    name: str
    campaign_id: str | None = None
    rules: dict[str, Any] | None = None
    destination_phone: str
    destination_label: str | None = None
    priority: int
    active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Voice AI Campaign Config
# ---------------------------------------------------------------------------

AmdStrategy = Literal["vapi", "twilio", "none"]


class VoiceAiCampaignConfigRequest(BaseModel):
    voice_assistant_id: str | None = None
    voice_phone_number_id: str | None = None
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


class VoiceAiCampaignConfigResponse(BaseModel):
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
# Call Logs
# ---------------------------------------------------------------------------

CallDirection = Literal["outbound", "inbound"]
CallType = Literal["outbound", "inbound", "callback"]
CallStatus = Literal["queued", "ringing", "in-progress", "forwarding", "ended"]
CallOutcome = Literal[
    "qualified_transfer",
    "not_qualified",
    "callback_requested",
    "voicemail_left",
    "no_answer",
    "busy",
    "error",
]


class CallLogResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str | None = None
    voice_assistant_id: str | None = None
    voice_phone_number_id: str | None = None
    vapi_call_id: str | None = None
    twilio_call_sid: str | None = None
    direction: str | None = None
    call_type: str | None = None
    customer_number: str | None = None
    from_number: str | None = None
    status: str | None = None
    ended_reason: str | None = None
    outcome: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    transcript: str | None = None
    transcript_messages: list[dict[str, Any]] | None = None
    recording_url: str | None = None
    structured_data: dict[str, Any] | None = None
    analysis_summary: str | None = None
    success_evaluation: str | None = None
    cost_breakdown: dict[str, Any] | None = None
    cost_total: float | None = None
    campaign_id: str | None = None
    campaign_lead_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Call Initiation
# ---------------------------------------------------------------------------


class CallInitiateRequest(BaseModel):
    voice_assistant_id: str
    voice_phone_number_id: str
    customer_number: str
    metadata: dict[str, Any] | None = None
    assistant_overrides: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}
