from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class BusinessInfoInput(BaseModel):
    business_name: str
    business_identity: str = Field(description="direct_customer, isv_reseller_or_partner, or unknown")
    business_type: str = Field(description="e.g., Corporation, LLC, Sole Proprietorship")
    business_industry: str = Field(description="e.g., TECHNOLOGY, HEALTHCARE, FINTECH")
    business_registration_identifier: str = Field(description="e.g., EIN, CBN, VAT")
    business_registration_number: str
    business_regions_of_operation: str = Field(description="e.g., USA_AND_CANADA, EUROPE")
    website_url: str
    social_media_profile_urls: str | None = None

    model_config = {"extra": "forbid"}


class AuthorizedRepresentativeInput(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone_number: str = Field(description="E.164 format with country code, e.g., +11234567890")
    business_title: str
    job_position: str = Field(description="Director, GM, VP, CEO, CFO, General Counsel, or Other")

    model_config = {"extra": "forbid"}


class BusinessAddressInput(BaseModel):
    customer_name: str
    street: str = Field(description="Physical street address (no PO Boxes)")
    city: str
    region: str = Field(description="State or region")
    postal_code: str
    iso_country: str = Field(description="2-letter ISO country code")
    street_secondary: str | None = Field(default=None, description="Apt, Suite, etc.")

    model_config = {"extra": "forbid"}


class RegisterCompanyRequest(BaseModel):
    """Trigger Trust Hub registration for a company."""
    partner_id: str
    registration_types: list[str] = Field(
        description="List of registration types: customer_profile, shaken_stir, a2p_campaign, cnam. "
                    "customer_profile is always required and will be auto-included if missing."
    )
    notification_email: str = Field(description="Email for Twilio status notifications")
    business_info: BusinessInfoInput
    authorized_representative: AuthorizedRepresentativeInput
    authorized_representative_2: AuthorizedRepresentativeInput | None = None
    address: BusinessAddressInput

    model_config = {"extra": "forbid"}


class AssignPhoneNumberRequest(BaseModel):
    """Assign a phone number to a company's Trust Hub bundles."""
    phone_number_sid: str = Field(description="Twilio phone number SID (PN-prefix)")
    partner_id: str
    bundle_types: list[str] = Field(
        default=["customer_profile"],
        description="Bundle types to assign to: customer_profile, shaken_stir, a2p_campaign, cnam"
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class RegistrationResponse(BaseModel):
    id: str
    brand_id: str
    partner_id: str
    registration_type: str
    status: str
    bundle_sid: str | None = None
    policy_sid: str | None = None
    evaluation_status: str | None = None
    evaluation_results: dict | list | None = None
    error_details: dict | None = None
    submitted_at: str | None = None
    approved_at: str | None = None
    rejected_at: str | None = None
    created_at: str
    updated_at: str


class RegisterCompanyResponse(BaseModel):
    registrations: list[RegistrationResponse]


class PhoneNumberAssignmentResponse(BaseModel):
    phone_number_sid: str
    assignments: list[dict]
