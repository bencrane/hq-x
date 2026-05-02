"""Dub.co internal REST wrapper.

Mounted at `/api/v1/dub/*`. Single chokepoint for retries, error mapping,
metrics, and tenant_id stamping. Reads are JWT-gated; writes are operator-
only. Upstream Dub auth failures are translated to 502 — never 401 — so
callers don't think their JWT was rejected.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.config import settings
from app.dmaas import dub_links as dub_links_repo
from app.dmaas import dub_webhooks_repo
from app.models.dub import (
    DubAnalyticsResponse,
    DubEventListResponse,
    DubLinkCreateRequest,
    DubLinkListResponse,
    DubLinkPersistedRow,
    DubLinkResponse,
    DubLinkUpdateRequest,
    DubLinkWithJoin,
    DubLinkWithJoinListResponse,
    dub_link_from_api,
)
from app.observability import incr_metric
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dub", tags=["dub"])


def _dub_api_key() -> str:
    if settings.DUB_API_KEY is None:
        raise HTTPException(
            503,
            {
                "error": "dub_not_configured",
                "message": "DUB_API_KEY not set",
            },
        )
    return settings.DUB_API_KEY.get_secret_value()


def _dub_base_url() -> str | None:
    return settings.DUB_API_BASE_URL


def _raise_provider_error(operation: str, exc: DubProviderError) -> None:
    """Translate a DubProviderError into the right HTTPException for callers.

    Dub auth failures (401/403) become 502 dub_auth_failed — propagating 401
    would mislead the caller into thinking their Supabase JWT was rejected.
    """
    incr_metric(
        "dub.api.error",
        operation=operation,
        category=exc.category,
        status=str(exc.status) if exc.status is not None else "none",
        code=exc.code or "none",
    )
    if exc.status in (401, 403):
        raise HTTPException(
            502,
            {
                "error": "dub_auth_failed",
                "message": "DUB_API_KEY rejected — operator must rotate",
            },
        )
    if exc.status == 404:
        raise HTTPException(
            404,
            {
                "error": "dub_resource_not_found",
                "code": exc.code,
                "message": str(exc),
            },
        )
    if exc.status == 409:
        raise HTTPException(
            409,
            {
                "error": "dub_conflict",
                "code": exc.code,
                "message": str(exc),
            },
        )
    if exc.status in (400, 422):
        raise HTTPException(
            400,
            {
                "error": "dub_bad_request",
                "code": exc.code,
                "message": str(exc),
                "doc_url": exc.doc_url,
            },
        )
    if exc.category == "transient":
        raise HTTPException(
            503,
            {
                "error": "dub_unavailable",
                "message": str(exc),
            },
        )
    raise HTTPException(
        502,
        {
            "error": "dub_upstream_error",
            "code": exc.code,
            "status": exc.status,
            "message": str(exc),
        },
    )


def _record_to_persisted(row: dub_links_repo.DubLinkRecord) -> DubLinkPersistedRow:
    return DubLinkPersistedRow(
        id=row.id,
        dub_link_id=row.dub_link_id,
        dub_external_id=row.dub_external_id,
        dub_short_url=row.dub_short_url,
        dub_domain=row.dub_domain,
        dub_key=row.dub_key,
        destination_url=row.destination_url,
        dmaas_design_id=row.dmaas_design_id,
        direct_mail_piece_id=row.direct_mail_piece_id,
        brand_id=row.brand_id,
        dub_folder_id=row.dub_folder_id,
        dub_tag_ids=row.dub_tag_ids,
        attribution_context=row.attribution_context,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@router.post("/links", response_model=DubLinkResponse, status_code=201)
async def create_link_route(
    body: DubLinkCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DubLinkResponse:
    api_key = _dub_api_key()
    domain = body.domain or settings.DUB_DEFAULT_DOMAIN
    tenant_id = body.tenant_id or settings.DUB_DEFAULT_TENANT_ID

    try:
        payload = dub_client.create_link(
            api_key=api_key,
            url=str(body.url),
            domain=domain,
            key=body.key,
            external_id=body.external_id,
            tenant_id=tenant_id,
            tag_ids=body.tag_ids,
            tag_names=body.tag_names,
            comments=body.comments,
            track_conversion=body.track_conversion,
            expires_at=body.expires_at.isoformat() if body.expires_at else None,
            expired_url=body.expired_url,
            ios=body.ios,
            android=body.android,
            geo=body.geo,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("create_link", exc)
        raise  # unreachable

    incr_metric("dub.link.created")

    # Best-effort persist of the join row. We don't roll back the Dub link
    # if the DB write fails — the dub side is the source of truth, and an
    # orphaned dmaas_dub_links absence is preferable to an orphaned link
    # that we cancelled but actually printed somewhere.
    try:
        await dub_links_repo.insert_dub_link(
            dub_link_id=str(payload.get("id", "")),
            dub_external_id=payload.get("externalId"),
            dub_short_url=str(payload.get("shortLink") or payload.get("url") or ""),
            dub_domain=str(payload.get("domain", "")),
            dub_key=str(payload.get("key", "")),
            destination_url=str(payload.get("url") or body.url),
            dmaas_design_id=body.dmaas_design_id,
            direct_mail_piece_id=body.direct_mail_piece_id,
            brand_id=body.brand_id,
            attribution_context=body.attribution_context,
            created_by_user_id=user.business_user_id,
            channel_campaign_step_id=None,
            recipient_id=None,
        )
    except Exception:
        logger.exception(
            "dmaas_dub_links insert failed for dub_link_id=%s", payload.get("id")
        )
        incr_metric("dub.link.persist_failed")

    return dub_link_from_api(payload)


@router.get("/links", response_model=DubLinkListResponse)
async def list_links_route(
    tenant_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    sort_by: str | None = Query(default=None),
    sort_order: str | None = Query(default=None),
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubLinkListResponse:
    api_key = _dub_api_key()
    try:
        rows = dub_client.list_links(
            api_key=api_key,
            tenant_id=tenant_id,
            search=search,
            domain=domain,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("list_links", exc)
        raise

    items = [dub_link_from_api(r) for r in rows]
    return DubLinkListResponse(count=len(items), links=items)


@router.get("/links/by-external-id/{external_id}", response_model=DubLinkResponse)
async def get_link_by_external_id_route(
    external_id: str,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubLinkResponse:
    api_key = _dub_api_key()
    try:
        payload = dub_client.get_link_by_external_id(
            api_key=api_key,
            external_id=external_id,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("get_link_by_external_id", exc)
        raise
    return dub_link_from_api(payload)


@router.get("/links/by-design/{design_id}", response_model=DubLinkWithJoinListResponse)
async def list_links_for_design_route(
    design_id: UUID,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubLinkWithJoinListResponse:
    """List Dub links minted in the context of a specific DMaaS design.

    Reads from `dmaas_dub_links`, then enriches each with the live Dub link
    when reachable. Upstream Dub failures degrade gracefully — the cached
    persisted row is always returned.
    """
    api_key = _dub_api_key()
    persisted = await dub_links_repo.list_dub_links_for_design(design_id)

    items: list[DubLinkWithJoin] = []
    for row in persisted:
        link: DubLinkResponse | None = None
        try:
            payload = dub_client.get_link(
                api_key=api_key,
                link_id=row.dub_link_id,
                base_url=_dub_base_url(),
            )
            link = dub_link_from_api(payload)
        except DubProviderError as exc:
            incr_metric(
                "dub.api.error",
                operation="get_link_for_design",
                category=exc.category,
                status=str(exc.status) if exc.status is not None else "none",
            )
            link = None
        items.append(DubLinkWithJoin(persisted=_record_to_persisted(row), link=link))

    return DubLinkWithJoinListResponse(count=len(items), items=items)


# NOTE: bulk routes are declared BEFORE /links/{link_id} so FastAPI's
# registration-order matching dispatches `/links/bulk` to its dedicated
# handler instead of treating "bulk" as a link id.


class BulkLinkSpec(BaseModel):
    url: str
    domain: str | None = None
    key: str | None = None
    external_id: str | None = None
    tenant_id: str | None = None
    folder_id: str | None = None
    tag_ids: list[str] | None = None
    tag_names: list[str] | None = None
    track_conversion: bool | None = None
    expires_at: str | None = None
    expired_url: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_term: str | None = None
    utm_content: str | None = None


class BulkLinkCreateRequest(BaseModel):
    links: list[BulkLinkSpec] = Field(min_length=1, max_length=100)


class BulkLinkUpdateRequest(BaseModel):
    link_ids: list[str] = Field(min_length=1, max_length=100)
    fields: dict[str, Any]


@router.post("/links/bulk", status_code=201)
async def bulk_create_links_route(
    body: BulkLinkCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        results = dub_client.bulk_create_links(
            api_key=api_key,
            links=[item.model_dump(exclude_none=True) for item in body.links],
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("bulk_create_links", exc)
        raise

    incr_metric("dub.link.bulk_created", count=str(len(results)))
    return {"count": len(results), "results": results}


@router.patch("/links/bulk")
async def bulk_update_links_route(
    body: BulkLinkUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        results = dub_client.bulk_update_links(
            api_key=api_key,
            link_ids=body.link_ids,
            fields=body.fields,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("bulk_update_links", exc)
        raise

    incr_metric("dub.link.bulk_updated", count=str(len(results)))
    return {"count": len(results), "results": results}


@router.delete("/links/bulk")
async def bulk_delete_links_route(
    link_ids: str = Query(..., description="comma-separated list of dub link ids"),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    ids = [s.strip() for s in link_ids.split(",") if s.strip()]
    if not ids:
        raise HTTPException(400, {"error": "missing_link_ids"})
    api_key = _dub_api_key()
    try:
        result = dub_client.bulk_delete_links(
            api_key=api_key,
            link_ids=ids,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("bulk_delete_links", exc)
        raise

    deleted = result.get("deletedCount") if isinstance(result, dict) else None
    if not isinstance(deleted, int):
        deleted = len(ids)
    incr_metric("dub.link.bulk_deleted", count=str(deleted))
    return {"deleted_count": deleted, "raw": result}


@router.get("/links/{link_id}", response_model=DubLinkResponse)
async def get_link_route(
    link_id: str,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubLinkResponse:
    api_key = _dub_api_key()
    try:
        payload = dub_client.get_link(
            api_key=api_key,
            link_id=link_id,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("get_link", exc)
        raise
    return dub_link_from_api(payload)


@router.patch("/links/{link_id}", response_model=DubLinkResponse)
async def update_link_route(
    link_id: str,
    body: DubLinkUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> DubLinkResponse:
    api_key = _dub_api_key()
    fields = body.model_dump(exclude_none=True)
    # HttpUrl / datetime → str for the wire.
    if "url" in fields and fields["url"] is not None:
        fields["url"] = str(fields["url"])
    if "expires_at" in fields and hasattr(fields["expires_at"], "isoformat"):
        fields["expires_at"] = fields["expires_at"].isoformat()

    try:
        payload = dub_client.update_link(
            api_key=api_key,
            link_id=link_id,
            fields=fields,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("update_link", exc)
        raise

    incr_metric("dub.link.updated")
    return dub_link_from_api(payload)


class _DeleteResponse(BaseModel):
    id: str
    deleted: bool
    archived: bool


@router.delete("/links/{link_id}", response_model=_DeleteResponse)
async def delete_link_route(
    link_id: str,
    hard: bool = Query(default=False),
    _user: UserContext = Depends(require_operator),
) -> _DeleteResponse:
    """Default soft-delete (archive). `?hard=true` performs a real delete."""
    api_key = _dub_api_key()
    try:
        if hard:
            dub_client.delete_link(
                api_key=api_key,
                link_id=link_id,
                base_url=_dub_base_url(),
            )
            incr_metric("dub.link.deleted", hard="true")
            return _DeleteResponse(id=link_id, deleted=True, archived=False)

        dub_client.update_link(
            api_key=api_key,
            link_id=link_id,
            fields={"archived": True},
            base_url=_dub_base_url(),
        )
        incr_metric("dub.link.deleted", hard="false")
        return _DeleteResponse(id=link_id, deleted=False, archived=True)
    except DubProviderError as exc:
        _raise_provider_error("delete_link", exc)
        raise


# ---------------------------------------------------------------------------
# Analytics + events
# ---------------------------------------------------------------------------


@router.get("/analytics", response_model=DubAnalyticsResponse)
async def retrieve_analytics_route(
    event: str = Query(default="clicks"),
    group_by: str = Query(default="count"),
    interval: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    link_id: str | None = Query(default=None),
    external_id: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    key: str | None = Query(default=None),
    country: str | None = Query(default=None),
    city: str | None = Query(default=None),
    region: str | None = Query(default=None),
    continent: str | None = Query(default=None),
    device: str | None = Query(default=None),
    browser: str | None = Query(default=None),
    os: str | None = Query(default=None),
    referer: str | None = Query(default=None),
    referer_url: str | None = Query(default=None),
    url: str | None = Query(default=None),
    qr: bool | None = Query(default=None),
    trigger: str | None = Query(default=None),
    folder_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubAnalyticsResponse:
    api_key = _dub_api_key()
    try:
        data: Any = dub_client.retrieve_analytics(
            api_key=api_key,
            event=event,  # type: ignore[arg-type]
            group_by=group_by,
            interval=interval,
            start=start,
            end=end,
            link_id=link_id,
            external_id=external_id,
            tenant_id=tenant_id or settings.DUB_DEFAULT_TENANT_ID,
            domain=domain,
            key=key,
            country=country,
            city=city,
            region=region,
            continent=continent,
            device=device,
            browser=browser,
            os=os,
            referer=referer,
            referer_url=referer_url,
            url=url,
            qr=qr,
            trigger=trigger,
            folder_id=folder_id,
            customer_id=customer_id,
            timezone=timezone,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("retrieve_analytics", exc)
        raise

    incr_metric("dub.analytics.queried", event=event, group_by=group_by)
    return DubAnalyticsResponse(
        event=event,
        group_by=group_by,
        interval=interval,
        data=data,
    )


@router.get("/events", response_model=DubEventListResponse)
async def list_events_route(
    event: str = Query(default="clicks"),
    interval: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    link_id: str | None = Query(default=None),
    external_id: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    domain: str | None = Query(default=None),
    key: str | None = Query(default=None),
    country: str | None = Query(default=None),
    city: str | None = Query(default=None),
    region: str | None = Query(default=None),
    continent: str | None = Query(default=None),
    device: str | None = Query(default=None),
    browser: str | None = Query(default=None),
    os: str | None = Query(default=None),
    referer: str | None = Query(default=None),
    referer_url: str | None = Query(default=None),
    url: str | None = Query(default=None),
    qr: bool | None = Query(default=None),
    trigger: str | None = Query(default=None),
    folder_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DubEventListResponse:
    api_key = _dub_api_key()
    try:
        rows = dub_client.list_events(
            api_key=api_key,
            event=event,  # type: ignore[arg-type]
            interval=interval,
            start=start,
            end=end,
            link_id=link_id,
            external_id=external_id,
            tenant_id=tenant_id or settings.DUB_DEFAULT_TENANT_ID,
            page=page,
            domain=domain,
            key=key,
            country=country,
            city=city,
            region=region,
            continent=continent,
            device=device,
            browser=browser,
            os=os,
            referer=referer,
            referer_url=referer_url,
            url=url,
            qr=qr,
            trigger=trigger,
            folder_id=folder_id,
            customer_id=customer_id,
            timezone=timezone,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("list_events", exc)
        raise

    incr_metric("dub.events.queried", event=event)
    return DubEventListResponse(event=event, count=len(rows), events=rows)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


class FolderListResponse(BaseModel):
    count: int
    folders: list[dict[str, Any]]


class FolderCreateRequest(BaseModel):
    name: str
    access_level: str = "write"


class FolderUpdateRequest(BaseModel):
    name: str | None = None
    access_level: str | None = None


@router.get("/folders", response_model=FolderListResponse)
async def list_folders_route(
    _user: UserContext = Depends(verify_supabase_jwt),
) -> FolderListResponse:
    api_key = _dub_api_key()
    try:
        rows = dub_client.list_folders(api_key=api_key, base_url=_dub_base_url())
    except DubProviderError as exc:
        _raise_provider_error("list_folders", exc)
        raise
    return FolderListResponse(count=len(rows), folders=rows)


@router.post("/folders", status_code=201)
async def create_folder_route(
    body: FolderCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.create_folder(
            api_key=api_key,
            name=body.name,
            access_level=body.access_level,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("create_folder", exc)
        raise


@router.get("/folders/{folder_id}")
async def get_folder_route(
    folder_id: str,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.get_folder(
            api_key=api_key, folder_id=folder_id, base_url=_dub_base_url()
        )
    except DubProviderError as exc:
        _raise_provider_error("get_folder", exc)
        raise


@router.patch("/folders/{folder_id}")
async def update_folder_route(
    folder_id: str,
    body: FolderUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.update_folder(
            api_key=api_key,
            folder_id=folder_id,
            name=body.name,
            access_level=body.access_level,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("update_folder", exc)
        raise


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder_route(
    folder_id: str,
    _user: UserContext = Depends(require_operator),
) -> None:
    api_key = _dub_api_key()
    try:
        dub_client.delete_folder(
            api_key=api_key, folder_id=folder_id, base_url=_dub_base_url()
        )
    except DubProviderError as exc:
        _raise_provider_error("delete_folder", exc)
        raise


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagListResponse(BaseModel):
    count: int
    tags: list[dict[str, Any]]


class TagCreateRequest(BaseModel):
    name: str
    color: str | None = None


class TagUpdateRequest(BaseModel):
    name: str | None = None
    color: str | None = None


@router.get("/tags", response_model=TagListResponse)
async def list_tags_route(
    _user: UserContext = Depends(verify_supabase_jwt),
) -> TagListResponse:
    api_key = _dub_api_key()
    try:
        rows = dub_client.list_tags(api_key=api_key, base_url=_dub_base_url())
    except DubProviderError as exc:
        _raise_provider_error("list_tags", exc)
        raise
    return TagListResponse(count=len(rows), tags=rows)


@router.post("/tags", status_code=201)
async def create_tag_route(
    body: TagCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.create_tag(
            api_key=api_key,
            name=body.name,
            color=body.color,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("create_tag", exc)
        raise


@router.patch("/tags/{tag_id}")
async def update_tag_route(
    tag_id: str,
    body: TagUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.update_tag(
            api_key=api_key,
            tag_id=tag_id,
            name=body.name,
            color=body.color,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("update_tag", exc)
        raise


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag_route(
    tag_id: str,
    _user: UserContext = Depends(require_operator),
) -> None:
    api_key = _dub_api_key()
    try:
        dub_client.delete_tag(
            api_key=api_key, tag_id=tag_id, base_url=_dub_base_url()
        )
    except DubProviderError as exc:
        _raise_provider_error("delete_tag", exc)
        raise


# ---------------------------------------------------------------------------
# Webhooks (CRUD against Dub + local mirror)
# ---------------------------------------------------------------------------


class WebhookListResponse(BaseModel):
    count: int
    webhooks: list[dict[str, Any]]


class WebhookCreateRequest(BaseModel):
    name: str
    url: str
    triggers: list[str] = Field(min_length=1)
    secret: str | None = None
    link_ids: list[str] | None = None
    tag_ids: list[str] | None = None


class WebhookUpdateRequest(BaseModel):
    name: str | None = None
    url: str | None = None
    triggers: list[str] | None = None
    secret: str | None = None
    link_ids: list[str] | None = None
    tag_ids: list[str] | None = None
    disabled: bool | None = None


def _hash_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@router.get("/webhooks", response_model=WebhookListResponse)
async def list_webhooks_route(
    _user: UserContext = Depends(require_operator),
) -> WebhookListResponse:
    api_key = _dub_api_key()
    try:
        rows = dub_client.list_webhooks(api_key=api_key, base_url=_dub_base_url())
    except DubProviderError as exc:
        _raise_provider_error("list_webhooks", exc)
        raise
    return WebhookListResponse(count=len(rows), webhooks=rows)


@router.post("/webhooks", status_code=201)
async def create_webhook_route(
    body: WebhookCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        payload = dub_client.create_webhook(
            api_key=api_key,
            name=body.name,
            url=body.url,
            triggers=body.triggers,
            secret=body.secret,
            link_ids=body.link_ids,
            tag_ids=body.tag_ids,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("create_webhook", exc)
        raise

    dub_id = str(payload.get("id") or "")
    if dub_id:
        try:
            await dub_webhooks_repo.insert_dub_webhook(
                dub_webhook_id=dub_id,
                name=body.name,
                receiver_url=body.url,
                triggers=body.triggers,
                environment=settings.APP_ENV,
                secret_hash=_hash_secret(body.secret),
            )
        except Exception:
            logger.exception(
                "dub_webhooks insert failed for dub_webhook_id=%s", dub_id
            )
            incr_metric("dub.webhook.persist_failed")
    incr_metric("dub.webhook.created")
    return payload


@router.get("/webhooks/{webhook_id}")
async def get_webhook_route(
    webhook_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.get_webhook(
            api_key=api_key, webhook_id=webhook_id, base_url=_dub_base_url()
        )
    except DubProviderError as exc:
        _raise_provider_error("get_webhook", exc)
        raise


@router.patch("/webhooks/{webhook_id}")
async def update_webhook_route(
    webhook_id: str,
    body: WebhookUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        return dub_client.update_webhook(
            api_key=api_key,
            webhook_id=webhook_id,
            name=body.name,
            url=body.url,
            triggers=body.triggers,
            secret=body.secret,
            link_ids=body.link_ids,
            tag_ids=body.tag_ids,
            disabled=body.disabled,
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("update_webhook", exc)
        raise


@router.delete("/webhooks/{webhook_id}", status_code=200)
async def delete_webhook_route(
    webhook_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _dub_api_key()
    try:
        dub_client.delete_webhook(
            api_key=api_key, webhook_id=webhook_id, base_url=_dub_base_url()
        )
    except DubProviderError as exc:
        _raise_provider_error("delete_webhook", exc)
        raise

    try:
        await dub_webhooks_repo.deactivate_dub_webhook(webhook_id)
    except Exception:
        logger.exception(
            "dub_webhooks deactivate failed for dub_webhook_id=%s", webhook_id
        )
        incr_metric("dub.webhook.deactivate_failed")
    incr_metric("dub.webhook.deleted")
    return {"id": webhook_id, "deleted": True}


@router.post("/webhooks/bootstrap", status_code=200)
async def bootstrap_webhook_route(
    receiver_url: str = Query(..., description="full URL to our webhook receiver"),
    triggers: list[str] | None = Query(
        default=None,
        description=(
            "dub trigger names; default = link.clicked,lead.created,sale.created"
        ),
    ),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    """Idempotent: creates the webhook in Dub if no active row exists for
    (APP_ENV, receiver_url); otherwise returns the existing local record.
    """
    from app.services import dub_webhook_bootstrap

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url=receiver_url,
        triggers=triggers,
    )
    return {
        "dub_webhook_id": rec.dub_webhook_id,
        "name": rec.name,
        "receiver_url": rec.receiver_url,
        "triggers": rec.triggers,
        "environment": rec.environment,
        "is_active": rec.is_active,
    }
