from __future__ import annotations

from pydantic import BaseModel, Field


class SendSmsRequest(BaseModel):
    to: str = Field(description="E.164 phone number to send to")
    body: str | None = Field(default=None, description="Text content of the message (up to 1600 chars)")
    from_number: str | None = Field(default=None, description="E.164 sender number. If omitted, uses Messaging Service.")
    messaging_service_sid: str | None = Field(default=None, description="Messaging Service SID for sender pool routing")
    media_url: list[str] | None = Field(default=None, description="URLs of media to attach (MMS). Max 10.")
    # Campaign context (optional)
    campaign_id: str | None = None
    campaign_lead_id: str | None = None

    model_config = {"extra": "forbid"}


class SendSmsResponse(BaseModel):
    message_sid: str
    status: str
    direction: str
    from_number: str
    to: str


class SmsMessageResponse(BaseModel):
    id: str
    message_sid: str
    direction: str
    from_number: str
    to_number: str
    body: str | None
    status: str
    error_code: int | None
    error_message: str | None
    num_segments: int | None
    num_media: int | None
    media_urls: list[str] | None
    date_sent: str | None
    created_at: str
    updated_at: str
