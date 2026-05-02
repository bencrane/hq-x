"""Pydantic request/response shapes for /api/v1/entri/*.

The session response is structured as the literal config object the
frontend hands to `window.entri.showEntri(...)` — keys deliberately match
Entri's camelCase SDK contract instead of our usual snake_case so the
frontend can pass it through verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EntriDnsRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["A", "AAAA", "CNAME", "CAA", "MX", "TXT", "NS"]
    host: str
    value: str
    ttl: int = 300
    application_url: str | None = Field(default=None, alias="applicationUrl")
    priority: int | None = None


class EntriSessionRequest(BaseModel):
    """Body for POST /api/v1/entri/session."""

    domain: str = Field(..., description='Customer-owned domain, e.g. "acme.com"')
    subdomain: str | None = Field(
        default=None, description='Subdomain prefix, e.g. "qr". Omit for root-domain flow.'
    )
    channel_campaign_step_id: UUID | None = Field(
        default=None,
        description="Step the domain serves. None for org-level domains.",
    )
    use_root_domain: bool = False
    application_url_path: str | None = Field(
        default=None,
        description=(
            "Optional path appended to ENTRI_APPLICATION_URL_BASE. Defaults "
            'to "/lp/<step_id>" when channel_campaign_step_id is set.'
        ),
    )


class EntriSessionResponse(BaseModel):
    """Bundle the frontend hands directly to `window.entri.showEntri()`.

    `session_id` and `application_url` are *our* fields (used by
    /api/v1/entri/success). Everything else matches Entri's SDK contract.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: UUID
    application_id: str = Field(..., alias="applicationId")
    token: str
    dns_records: list[EntriDnsRecord] = Field(..., alias="dnsRecords")
    user_id: str = Field(..., alias="userId")
    application_url: str = Field(..., alias="applicationUrl")
    prefilled_domain: str = Field(..., alias="prefilledDomain")
    default_subdomain: str | None = Field(default=None, alias="defaultSubdomain")
    power: bool = True
    secure_root_domain: bool = Field(default=False, alias="secureRootDomain")


class EntriSuccessRequest(BaseModel):
    """Body for POST /api/v1/entri/success — frontend's `onSuccess` payload."""

    session_id: UUID
    domain: str
    setup_type: str | None = None
    provider: str | None = None
    job_id: str | None = None


class EntriDomainConnectionResponse(BaseModel):
    id: UUID
    organization_id: UUID
    channel_campaign_step_id: UUID | None
    domain: str
    is_root_domain: bool
    application_url: str
    state: str
    provider: str | None
    setup_type: str | None
    propagation_status: str | None
    power_status: str | None
    secure_status: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class EntriDomainListResponse(BaseModel):
    domains: list[EntriDomainConnectionResponse]


class EntriEligibilityResponse(BaseModel):
    domain: str
    eligible: bool
    raw: dict[str, Any] = {}
