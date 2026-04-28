"""Pydantic request/response shapes for the direct-mail router.

Most piece-create payloads are passed through to Lob mostly verbatim; the
local model is a thin schema check plus a Lob-agnostic envelope (idempotency
+ verify-gate flag). Single-tenant — no `org_id` field.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

PieceType = Literal["postcard", "letter", "self_mailer", "snap_pack", "booklet"]
IdempotencyLocation = Literal["header", "query"]


class DirectMailAddressVerificationUSRequest(BaseModel):
    payload: dict[str, Any]


class DirectMailAddressVerificationUSBulkRequest(BaseModel):
    payload: dict[str, Any]


class DirectMailAddressVerificationResponse(BaseModel):
    provider: Literal["lob"] = "lob"
    result: dict[str, Any]


class DirectMailPieceCreateRequest(BaseModel):
    payload: dict[str, Any] = Field(
        ...,
        description=(
            "Lob-shaped create payload. Passed through to the provider with minimal "
            "transformation. The recipient address is read from `payload.to`."
        ),
    )
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None
    skip_address_verification: bool = Field(
        default=False,
        description=(
            "When true, bypass the pre-send US address-verify gate. Use only for "
            "pieces with caller-trusted addresses (e.g. a saved-address ID)."
        ),
    )
    test_mode: bool = Field(
        default=False,
        description=(
            "When true, route the request through LOB_API_KEY_TEST instead of "
            "LOB_API_KEY. Lob bills $0 for test-mode pieces. The piece row is "
            "still persisted and is flagged with is_test_mode=true so reports "
            "can exclude it."
        ),
    )


class DirectMailPieceResponse(BaseModel):
    id: UUID
    provider_slug: Literal["lob"] = "lob"
    external_piece_id: str
    piece_type: PieceType
    status: str
    send_date: str | None = None
    cost_cents: int | None = None
    deliverability: str | None = None
    is_test_mode: bool = False
    metadata: dict[str, Any] | None = None
    raw_payload: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class DirectMailPieceListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[dict[str, Any]]
    next_url: str | None = None
    previous_url: str | None = None
    count: int | None = None


class DirectMailPieceCancelResponse(BaseModel):
    id: str
    deleted: bool = True
    raw_payload: dict[str, Any] | None = None


class DirectMailTemplateCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailTemplateUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailTemplateResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailTemplateListResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailTemplateDeleteResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailTemplateVersionCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailTemplateVersionUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailTemplateVersionResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailTemplateVersionListResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailTemplateVersionDeleteResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailAddressCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailAddressResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailAddressListResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailBuckslipCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailBuckslipUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailBuckslipOrderCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCardCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCardUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCardOrderCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCampaignCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCampaignUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCreativeCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailCreativeUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailUploadCreateRequest(BaseModel):
    payload: dict[str, Any]


class DirectMailUploadUpdateRequest(BaseModel):
    payload: dict[str, Any]


class DirectMailUploadExportCreateRequest(BaseModel):
    payload: dict[str, Any]


class DirectMailUploadResponse(BaseModel):
    raw_payload: dict[str, Any]


class DirectMailResourceProofCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailResourceProofUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailDomainCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailLinkCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailLinkUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailBillingGroupCreateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class DirectMailBillingGroupUpdateRequest(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str | None = None
    idempotency_location: IdempotencyLocation | None = None


class SuppressedAddressResponse(BaseModel):
    address_hash: str
    reason: str
    address_line1: str
    address_line2: str | None = None
    address_city: str
    address_state: str
    address_zip: str
    suppressed_at: str
    notes: str | None = None
