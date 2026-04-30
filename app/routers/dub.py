"""Dub.co internal REST wrapper.

Mounted at `/api/v1/dub/*`. Single chokepoint for retries, error mapping,
metrics, and tenant_id stamping. Reads are JWT-gated; writes are operator-
only. Upstream Dub auth failures are translated to 502 — never 401 — so
callers don't think their JWT was rejected.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.config import settings
from app.dmaas import dub_links as dub_links_repo
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
            base_url=_dub_base_url(),
        )
    except DubProviderError as exc:
        _raise_provider_error("list_events", exc)
        raise

    incr_metric("dub.events.queried", event=event)
    return DubEventListResponse(event=event, count=len(rows), events=rows)
