"""Pydantic models for the campaigns hierarchy.

Two layers:
  * Campaign — the umbrella outreach effort (org-scoped, brand-bound,
    channel-agnostic). Backed by business.campaigns.
  * ChannelCampaign — the per-channel execution unit underneath a
    campaign. Backed by business.channel_campaigns.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

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


CampaignStatus = Literal["draft", "active", "paused", "completed", "archived"]
ChannelCampaignStatus = Literal[
    "draft", "scheduled", "sending", "sent", "paused", "failed", "archived"
]
ChannelCampaignStepStatus = Literal[
    "pending", "scheduled", "activating", "sent", "failed", "cancelled", "archived"
]


# ── Campaign (umbrella) ────────────────────────────────────────────────────


class CampaignCreate(BaseModel):
    brand_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    start_date: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # When set, this campaign rolls up under a GTM initiative. The
    # materializer is the only writer that populates this in the
    # owned-brand pipeline; user-initiated campaigns leave it None.
    initiative_id: UUID | None = None

    model_config = {"extra": "forbid"}


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: CampaignStatus | None = None
    start_date: date | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class CampaignResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID
    name: str
    description: str | None = None
    status: CampaignStatus
    start_date: date | None = None
    metadata: dict[str, Any]
    initiative_id: UUID | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    model_config = {"extra": "forbid"}


# ── ChannelCampaign (per-channel execution) ────────────────────────────────


class ChannelCampaignCreate(BaseModel):
    campaign_id: UUID
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
    # Denormalized from parent campaigns.initiative_id. The materializer
    # sets this; the service layer also auto-fills it from the parent
    # campaign when not explicitly supplied.
    initiative_id: UUID | None = None

    model_config = {"extra": "forbid"}


class ChannelCampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    audience_spec_id: UUID | None = None
    audience_snapshot_count: int | None = Field(default=None, ge=0)
    start_offset_days: int | None = Field(default=None, ge=0)
    schedule_config: dict[str, Any] | None = None
    provider_config: dict[str, Any] | None = None
    design_id: UUID | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class ChannelCampaignResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    organization_id: UUID
    brand_id: UUID
    name: str
    channel: Channel
    provider: Provider
    audience_spec_id: UUID | None = None
    audience_snapshot_count: int | None = None
    status: ChannelCampaignStatus
    start_offset_days: int
    scheduled_send_at: datetime | None = None
    schedule_config: dict[str, Any]
    provider_config: dict[str, Any]
    design_id: UUID | None = None
    metadata: dict[str, Any]
    initiative_id: UUID | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    model_config = {"extra": "forbid"}


# ── ChannelCampaignStep (per-touch ordered execution under a channel campaign)


class ChannelCampaignStepCreate(BaseModel):
    step_order: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    delay_days_from_previous: int = Field(default=0, ge=0)
    creative_ref: UUID | None = None
    channel_specific_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class ChannelCampaignStepUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    delay_days_from_previous: int | None = Field(default=None, ge=0)
    creative_ref: UUID | None = None
    channel_specific_config: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class ChannelCampaignStepResponse(BaseModel):
    id: UUID
    channel_campaign_id: UUID
    campaign_id: UUID
    organization_id: UUID
    brand_id: UUID
    step_order: int
    name: str | None = None
    delay_days_from_previous: int
    scheduled_send_at: datetime | None = None
    creative_ref: UUID | None = None
    channel_specific_config: dict[str, Any]
    external_provider_id: str | None = None
    external_provider_metadata: dict[str, Any]
    status: ChannelCampaignStepStatus
    activated_at: datetime | None = None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ── Step landing-page config (rendered into the hosted landing page) ─────


# Form field name pattern: lowercase + digits + underscore. Names round-
# trip into landing_page_submissions.form_data as JSONB keys.
_FORM_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Field types we render inputs for in the V1 Jinja2 template. Anything
# outside this list (color picker, file upload, signature pad, …) is a
# future PR; reject it at submit-time so the operator gets a clear error
# rather than rendering a broken page.
FormFieldType = Literal[
    "text", "email", "tel", "url", "textarea", "select", "checkbox"
]
LandingPageCtaType = Literal["form", "phone", "email", "external_url"]


class FormField(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    type: FormFieldType
    required: bool = False
    placeholder: str | None = Field(default=None, max_length=200)
    # Used only for type='select'. Each option is a {value, label} pair.
    options: list[dict[str, str]] | None = None

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def _name_lowercase_snake(cls, v: str) -> str:
        if not _FORM_FIELD_NAME_RE.match(v):
            raise ValueError(
                "name must match [a-z][a-z0-9_]* (lowercase, digits, "
                "underscores; must start with a letter)"
            )
        return v


class FormSchema(BaseModel):
    fields: list[FormField] = Field(min_length=1, max_length=20)

    model_config = {"extra": "forbid"}

    @field_validator("fields")
    @classmethod
    def _names_unique(cls, v: list[FormField]) -> list[FormField]:
        seen: set[str] = set()
        for f in v:
            if f.name in seen:
                raise ValueError(f"duplicate field name {f.name!r}")
            seen.add(f.name)
        return v


class LandingPageCta(BaseModel):
    type: LandingPageCtaType
    label: str = Field(min_length=1, max_length=120)
    form_schema: FormSchema | None = None
    thank_you_message: str | None = Field(default=None, max_length=500)
    thank_you_redirect_url: str | None = Field(default=None, max_length=2048)
    # External URL the CTA navigates to (only when type='external_url').
    target_url: str | None = Field(default=None, max_length=2048)

    model_config = {"extra": "forbid"}

    @field_validator("thank_you_redirect_url", "target_url")
    @classmethod
    def _url_must_be_https(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("https://", "http://")):
            raise ValueError("URL must include http(s):// scheme")
        return v

    def model_post_init(self, _context) -> None:  # type: ignore[override]
        if self.type == "form" and self.form_schema is None:
            raise ValueError("cta.form_schema is required when cta.type='form'")
        if self.type == "external_url" and not self.target_url:
            raise ValueError(
                "cta.target_url is required when cta.type='external_url'"
            )


class StepLandingPageConfig(BaseModel):
    """Rendered into business.channel_campaign_steps.landing_page_config."""

    headline: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=2000)
    cta: LandingPageCta

    model_config = {"extra": "forbid"}


__all__ = [
    "VALID_CHANNEL_PROVIDER_PAIRS",
    "Channel",
    "Provider",
    "CampaignStatus",
    "ChannelCampaignStatus",
    "ChannelCampaignStepStatus",
    "CampaignCreate",
    "CampaignUpdate",
    "CampaignResponse",
    "ChannelCampaignCreate",
    "ChannelCampaignUpdate",
    "ChannelCampaignResponse",
    "ChannelCampaignStepCreate",
    "ChannelCampaignStepUpdate",
    "ChannelCampaignStepResponse",
    "FormFieldType",
    "LandingPageCtaType",
    "FormField",
    "FormSchema",
    "LandingPageCta",
    "StepLandingPageConfig",
]
