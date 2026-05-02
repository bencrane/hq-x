"""Pydantic request/response shapes for the /api/v1/dub/* router.

These are the *internal* contract — they wrap, but do not mirror, the
upstream Dub schema. We expose snake_case and only the fields we currently
use; passthrough of unknown Dub keys happens at the response layer via
`extra: dict[str, Any]` so we don't break when Dub adds fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

# ---------------------------------------------------------------------------
# Link request bodies
# ---------------------------------------------------------------------------


class DubLinkCreateRequest(BaseModel):
    """Body for POST /api/v1/dub/links.

    The `dmaas_design_id`, `direct_mail_piece_id`, `brand_id`, and
    `attribution_context` fields are NOT forwarded to Dub — they're
    persisted to `dmaas_dub_links` so we can attribute clicks back to the
    mailer they were printed on.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    domain: str | None = None
    key: str | None = None
    external_id: str | None = None
    tenant_id: str | None = None
    tag_ids: list[str] | None = None
    tag_names: list[str] | None = None
    track_conversion: bool = False
    comments: str | None = None
    expires_at: datetime | None = None
    expired_url: str | None = None
    ios: str | None = None
    android: str | None = None
    geo: dict[str, str] | None = None

    # Internal join fields — attached to dmaas_dub_links, NOT Dub.
    dmaas_design_id: UUID | None = None
    direct_mail_piece_id: UUID | None = None
    brand_id: UUID | None = None
    attribution_context: dict[str, Any] = Field(default_factory=dict)


class DubLinkUpdateRequest(BaseModel):
    """Body for PATCH /api/v1/dub/links/{id}. All fields optional."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl | None = None
    domain: str | None = None
    key: str | None = None
    external_id: str | None = None
    tenant_id: str | None = None
    tag_ids: list[str] | None = None
    tag_names: list[str] | None = None
    track_conversion: bool | None = None
    comments: str | None = None
    expires_at: datetime | None = None
    expired_url: str | None = None
    ios: str | None = None
    android: str | None = None
    geo: dict[str, str] | None = None
    archived: bool | None = None


# ---------------------------------------------------------------------------
# Link response shape
# ---------------------------------------------------------------------------


# Keys we explicitly model on DubLinkResponse. Anything else from Dub lands
# in the `extra` dict so we don't drop new fields silently.
_MODELED_DUB_KEYS = frozenset(
    {
        "id",
        "domain",
        "key",
        "url",
        "shortLink",
        "qrCode",
        "externalId",
        "tenantId",
        "trackConversion",
        "clicks",
        "leads",
        "sales",
        "createdAt",
        "updatedAt",
        "archived",
    }
)


class DubLinkResponse(BaseModel):
    """Snake_case projection of a Dub link object.

    Unknown Dub fields are preserved under `extra` so responses don't drop
    information when Dub evolves their schema.
    """

    id: str
    domain: str
    key: str
    url: str
    short_link: str | None = None
    qr_code: str | None = None
    external_id: str | None = None
    tenant_id: str | None = None
    track_conversion: bool = False
    clicks: int = 0
    leads: int = 0
    sales: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    archived: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class DubLinkListResponse(BaseModel):
    count: int
    links: list[DubLinkResponse]


def dub_link_from_api(payload: dict[str, Any]) -> DubLinkResponse:
    """Map a Dub link dict (camelCase) → DubLinkResponse (snake_case + extras)."""
    extras = {k: v for k, v in payload.items() if k not in _MODELED_DUB_KEYS}
    return DubLinkResponse(
        id=str(payload.get("id", "")),
        domain=str(payload.get("domain", "")),
        key=str(payload.get("key", "")),
        url=str(payload.get("url", "")),
        short_link=payload.get("shortLink"),
        qr_code=payload.get("qrCode"),
        external_id=payload.get("externalId"),
        tenant_id=payload.get("tenantId"),
        track_conversion=bool(payload.get("trackConversion", False)),
        clicks=int(payload.get("clicks") or 0),
        leads=int(payload.get("leads") or 0),
        sales=int(payload.get("sales") or 0),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
        archived=bool(payload.get("archived", False)),
        extra=extras,
    )


# ---------------------------------------------------------------------------
# Analytics + events
# ---------------------------------------------------------------------------


class DubAnalyticsResponse(BaseModel):
    """Wrapper around Dub's /analytics response.

    The `data` field shape varies by `group_by` (count → object, timeseries →
    list, country → list, etc.) so we pass it through verbatim.
    """

    event: str
    group_by: str
    interval: str | None = None
    data: Any


class DubEventListResponse(BaseModel):
    event: str
    count: int
    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Persistence shapes (dmaas_dub_links join)
# ---------------------------------------------------------------------------


class DubLinkPersistedRow(BaseModel):
    id: UUID
    dub_link_id: str
    dub_external_id: str | None
    dub_short_url: str
    dub_domain: str
    dub_key: str
    destination_url: str
    dmaas_design_id: UUID | None
    direct_mail_piece_id: UUID | None
    brand_id: UUID | None
    dub_folder_id: str | None = None
    dub_tag_ids: list[str] = Field(default_factory=list)
    attribution_context: dict[str, Any]
    created_at: str
    updated_at: str


class DubLinkWithJoin(BaseModel):
    """Returned by GET /api/v1/dub/links/by-design/{design_id}.

    `link` is the live Dub link (best-effort — when the upstream call fails
    we surface only the cached row from `dmaas_dub_links`).
    """

    persisted: DubLinkPersistedRow
    link: DubLinkResponse | None = None


class DubLinkWithJoinListResponse(BaseModel):
    count: int
    items: list[DubLinkWithJoin]
