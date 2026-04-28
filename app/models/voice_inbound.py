from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Inbound Phone Config
# ---------------------------------------------------------------------------

RoutingMode = Literal["static", "dynamic"]


class InboundPhoneConfigCreateRequest(BaseModel):
    phone_number: str
    phone_number_sid: str | None = None
    voice_assistant_id: str
    partner_id: str | None = None
    routing_mode: RoutingMode = "static"
    first_message_mode: str | None = None
    inbound_config: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class InboundPhoneConfigUpdateRequest(BaseModel):
    phone_number: str | None = None
    phone_number_sid: str | None = None
    voice_assistant_id: str | None = None
    partner_id: str | None = None
    routing_mode: RoutingMode | None = None
    first_message_mode: str | None = None
    inbound_config: dict[str, Any] | None = None
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


class InboundPhoneConfigResponse(BaseModel):
    id: str
    brand_id: str
    phone_number: str
    phone_number_sid: str | None = None
    voice_assistant_id: str
    partner_id: str | None = None
    routing_mode: str
    first_message_mode: str | None = None
    inbound_config: dict[str, Any] | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}
