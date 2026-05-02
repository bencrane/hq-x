from __future__ import annotations

from pydantic import BaseModel, Field


class OutboundCallRequest(BaseModel):
    to: str = Field(description="E.164 phone number to call")
    from_number: str = Field(description="E.164 caller ID (must be owned by org)")
    greeting_text: str | None = Field(
        default=None,
        description="Text spoken when call connects (before AMD). If omitted, a brief pause is used.",
    )
    voicemail_text: str | None = Field(
        default=None, description="Text-to-speech voicemail message if machine answers"
    )
    voicemail_audio_url: str | None = Field(
        default=None, description="Pre-recorded audio URL for voicemail drop"
    )
    human_message_text: str | None = Field(
        default=None,
        description="Text spoken if a human answers. Required for system-initiated calls with no rep.",
    )
    record: bool = Field(default=False, description="Whether to record the call")
    timeout: int = Field(default=30, ge=5, le=120, description="Ring timeout in seconds")
    campaign_id: str | None = None
    campaign_lead_id: str | None = None

    model_config = {"extra": "forbid"}


class OutboundCallResponse(BaseModel):
    call_sid: str
    status: str
    direction: str
    from_number: str
    to: str
    voice_session_id: str | None = None
