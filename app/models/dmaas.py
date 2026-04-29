"""Pydantic request/response shapes for the DMaaS router."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ScaffoldFormat = Literal["postcard", "letter", "self_mailer", "snap_pack", "booklet"]


# ---------------------------------------------------------------------------
# Scaffolds
# ---------------------------------------------------------------------------


class CompatibleSpec(BaseModel):
    """A (category, variant) pair from direct_mail_specs the scaffold supports."""

    model_config = ConfigDict(extra="forbid")
    category: str
    variant: str


class ScaffoldResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None
    format: ScaffoldFormat
    compatible_specs: list[CompatibleSpec]
    prop_schema: dict[str, Any]
    constraint_specification: dict[str, Any]
    preview_image_url: str | None
    vertical_tags: list[str]
    is_active: bool
    version_number: int
    created_at: str
    updated_at: str


class ScaffoldListResponse(BaseModel):
    count: int
    scaffolds: list[ScaffoldResponse]


class ScaffoldCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(..., pattern=r"^[a-z0-9_-]+$", min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    format: ScaffoldFormat
    compatible_specs: list[CompatibleSpec] = Field(default_factory=list)
    prop_schema: dict[str, Any] = Field(default_factory=dict)
    constraint_specification: dict[str, Any]
    preview_image_url: str | None = None
    vertical_tags: list[str] = Field(default_factory=list)
    is_active: bool = True
    # Optional placeholder content used to validate the constraint spec at
    # creation time. Maps element_name → content dict (with `intrinsic` for
    # size hints). One entry per compatible_specs item is best.
    placeholder_content: dict[str, Any] = Field(default_factory=dict)


class ScaffoldUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    compatible_specs: list[CompatibleSpec] | None = None
    prop_schema: dict[str, Any] | None = None
    constraint_specification: dict[str, Any] | None = None
    preview_image_url: str | None = None
    vertical_tags: list[str] | None = None
    is_active: bool | None = None
    placeholder_content: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Solver-result envelope (shared by validate / preview / save)
# ---------------------------------------------------------------------------


class ConstraintConflictResponse(BaseModel):
    constraint_index: int
    constraint_type: str
    phase: Literal["prevalidate", "linear", "validator"]
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class SolveResultResponse(BaseModel):
    is_valid: bool
    positions: dict[str, dict[str, float]]
    conflicts: list[ConstraintConflictResponse]
    canvas: dict[str, float] | None = None
    zones: dict[str, dict[str, float]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validate-constraints (no save)
# ---------------------------------------------------------------------------


class ValidateConstraintsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_category: str
    spec_variant: str
    constraint_specification: dict[str, Any]
    sample_content: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_category: str
    spec_variant: str
    placeholder_content: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


class DesignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scaffold_id: UUID
    spec_category: str
    spec_variant: str
    content_config: dict[str, Any]
    brand_id: UUID | None = None
    audience_template_id: UUID | None = None


class DesignUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_config: dict[str, Any]


class DesignResponse(BaseModel):
    id: UUID
    scaffold_id: UUID
    spec_category: str
    spec_variant: str
    content_config: dict[str, Any]
    resolved_positions: dict[str, Any]
    brand_id: UUID | None
    audience_template_id: UUID | None
    version_number: int
    created_at: str
    updated_at: str


class DesignListResponse(BaseModel):
    count: int
    designs: list[DesignResponse]


# ---------------------------------------------------------------------------
# Authoring sessions
# ---------------------------------------------------------------------------


class AuthoringSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scaffold_id: UUID | None = None
    prompt: str = Field(..., min_length=1)
    proposed_constraint_specification: dict[str, Any]
    accepted: bool = False
    notes: str | None = None


class AuthoringSessionResponse(BaseModel):
    id: UUID
    scaffold_id: UUID | None
    prompt: str
    proposed_constraint_specification: dict[str, Any]
    accepted: bool
    notes: str | None
    created_at: str


class AuthoringSessionListResponse(BaseModel):
    count: int
    sessions: list[AuthoringSessionResponse]
