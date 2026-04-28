from __future__ import annotations

from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# IVR Flow models
# ---------------------------------------------------------------------------


class IvrFlowCreate(BaseModel):
    name: str
    description: str | None = None
    default_voice: str = "Polly.Joanna-Generative"
    default_language: str = "en-US"
    lookup_type: str | None = None
    lookup_config: dict[str, Any] | None = None
    default_transfer_number: str | None = None
    transfer_timeout_seconds: int = 30
    recording_enabled: bool = False
    recording_consent_required: bool = True


class IvrFlowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    default_voice: str | None = None
    default_language: str | None = None
    lookup_type: str | None = None
    lookup_config: dict[str, Any] | None = None
    default_transfer_number: str | None = None
    transfer_timeout_seconds: int | None = None
    recording_enabled: bool | None = None
    recording_consent_required: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# IVR Flow Step models
# ---------------------------------------------------------------------------


class IvrFlowStepCreate(BaseModel):
    step_key: str
    step_type: str
    position: int
    say_text: str | None = None
    say_voice: str | None = None
    say_language: str | None = None
    gather_input: str | None = None
    gather_num_digits: int | None = None
    gather_timeout_seconds: int | None = 5
    gather_finish_on_key: str | None = "#"
    gather_max_retries: int | None = 2
    gather_invalid_message: str | None = None
    gather_validation_regex: str | None = None
    next_step_key: str | None = None
    branches: list[dict[str, Any]] | None = None
    transfer_number: str | None = None
    transfer_caller_id: str | None = None
    transfer_record: str | None = "do-not-record"
    record_max_length_seconds: int | None = 120
    record_play_beep: bool | None = True
    lookup_input_key: str | None = None
    lookup_store_key: str | None = "lookup_result"
    audio_url: str | None = None


class IvrFlowStepUpdate(BaseModel):
    step_key: str | None = None
    step_type: str | None = None
    position: int | None = None
    say_text: str | None = None
    say_voice: str | None = None
    say_language: str | None = None
    gather_input: str | None = None
    gather_num_digits: int | None = None
    gather_timeout_seconds: int | None = None
    gather_finish_on_key: str | None = None
    gather_max_retries: int | None = None
    gather_invalid_message: str | None = None
    gather_validation_regex: str | None = None
    next_step_key: str | None = None
    branches: list[dict[str, Any]] | None = None
    transfer_number: str | None = None
    transfer_caller_id: str | None = None
    transfer_record: str | None = None
    record_max_length_seconds: int | None = None
    record_play_beep: bool | None = None
    lookup_input_key: str | None = None
    lookup_store_key: str | None = None
    audio_url: str | None = None


# ---------------------------------------------------------------------------
# IVR Phone Config models
# ---------------------------------------------------------------------------


class IvrPhoneConfigCreate(BaseModel):
    phone_number: str
    phone_number_sid: str | None = None
    flow_id: str
    is_active: bool = True


class IvrPhoneConfigUpdate(BaseModel):
    flow_id: str | None = None
    is_active: bool | None = None
