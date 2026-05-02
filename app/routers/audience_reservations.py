"""Audience reservations REST API.

A reservation couples a paying organization (`business.organizations`) to
a frozen DEX `ops.audience_specs` row. The DEX spec id is the only
identifier — hq-x does NOT mint a second one. Cached fields
(`source_template_slug`, `source_template_id`, `audience_name`) make the
row self-describing without a DEX round-trip; the source of truth still
lives in DEX.

Composite read endpoints fan out to DEX for the live descriptor, count,
and per-member preview rows so downstream DM-creative consumers don't
need direct DEX access.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.db import get_db_connection
from app.services import dex_client

router = APIRouter(prefix="/api/audience-reservations", tags=["audience-reservations"])


# ────────────────────────────── schemas ──────────────────────────────


class CreateReservationRequest(BaseModel):
    data_engine_audience_id: UUID
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class ReservationResponse(BaseModel):
    id: UUID
    organization_id: UUID
    data_engine_audience_id: UUID
    source_template_slug: str
    source_template_id: UUID
    audience_name: str
    status: str
    reserved_at: datetime
    reserved_by_user_id: UUID | None
    notes: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CompositeAudienceResponse(BaseModel):
    reservation: ReservationResponse
    descriptor: dict[str, Any]
    count: dict[str, Any]


# ────────────────────────────── helpers ──────────────────────────────


_COLUMNS = """
    id, organization_id, data_engine_audience_id,
    source_template_slug, source_template_id, audience_name,
    status, reserved_at, reserved_by_user_id,
    notes, metadata, created_at, updated_at
"""


def _row_to_response(row: tuple, cols: list[str]) -> ReservationResponse:
    return ReservationResponse(**dict(zip(cols, row, strict=True)))


def _bearer_from_request(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


def _bad_request(error: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": error})


def _not_found(error: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": error})


def _bad_gateway(error: str, *, status_code: int | None = None, body: Any = None) -> HTTPException:
    detail: dict[str, Any] = {"error": error}
    if status_code is not None:
        detail["dex_status_code"] = status_code
    if body is not None:
        detail["dex_body"] = body
    return HTTPException(status_code=502, detail=detail)


def _translate_dex_error(exc: dex_client.DexCallError, *, on_404: str) -> HTTPException:
    if exc.status_code == 404:
        return _not_found(on_404)
    return _bad_gateway("dex_call_failed", status_code=exc.status_code, body=exc.body)


# ────────────────────────────── endpoints ────────────────────────────


@router.post(
    "",
    response_model=ReservationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_reservation(
    body: CreateReservationRequest,
    request: Request,
    user: UserContext = Depends(verify_supabase_jwt),
) -> ReservationResponse:
    if user.active_organization_id is None:
        raise _bad_request("organization_required")

    bearer = _bearer_from_request(request)

    try:
        descriptor = await dex_client.get_audience_descriptor(
            body.data_engine_audience_id, bearer_token=bearer,
        )
    except dex_client.DexCallError as exc:
        raise _translate_dex_error(exc, on_404="audience_not_found") from exc
    except dex_client.DexClientError as exc:
        raise _bad_gateway(f"dex_client_error: {exc}") from exc

    spec = descriptor.get("spec") or {}
    template = descriptor.get("template") or {}
    template_slug = template.get("slug")
    template_id = template.get("id")
    audience_name = spec.get("name") or template.get("name")
    if not (template_slug and template_id and audience_name):
        raise _bad_gateway("dex_descriptor_missing_fields")

    insert_sql = f"""
        INSERT INTO business.org_audience_reservations (
            organization_id, data_engine_audience_id,
            source_template_slug, source_template_id, audience_name,
            reserved_by_user_id, notes, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (organization_id, data_engine_audience_id)
        DO UPDATE SET
            notes = EXCLUDED.notes,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING {_COLUMNS}
    """

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                insert_sql,
                (
                    str(user.active_organization_id),
                    str(body.data_engine_audience_id),
                    template_slug,
                    str(template_id),
                    audience_name,
                    str(user.business_user_id),
                    body.notes,
                    json.dumps(body.metadata),
                ),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]
        await conn.commit()

    return _row_to_response(row, cols)


@router.get("", response_model=list[ReservationResponse])
async def list_reservations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(verify_supabase_jwt),
) -> list[ReservationResponse]:
    if user.active_organization_id is None:
        raise _bad_request("organization_required")

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.org_audience_reservations
                WHERE organization_id = %s
                ORDER BY reserved_at DESC
                LIMIT %s OFFSET %s
                """,
                (str(user.active_organization_id), limit, offset),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [_row_to_response(r, cols) for r in rows]


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation(
    reservation_id: UUID,
    user: UserContext = Depends(verify_supabase_jwt),
) -> ReservationResponse:
    row, cols = await _fetch_owned(reservation_id, user.active_organization_id)
    return _row_to_response(row, cols)


@router.get("/{reservation_id}/audience", response_model=CompositeAudienceResponse)
async def get_reservation_audience(
    reservation_id: UUID,
    request: Request,
    user: UserContext = Depends(verify_supabase_jwt),
) -> CompositeAudienceResponse:
    row, cols = await _fetch_owned(reservation_id, user.active_organization_id)
    reservation = _row_to_response(row, cols)
    bearer = _bearer_from_request(request)

    try:
        descriptor = await dex_client.get_audience_descriptor(
            reservation.data_engine_audience_id, bearer_token=bearer,
        )
        count = await dex_client.count_audience_members(
            reservation.data_engine_audience_id, bearer_token=bearer,
        )
    except dex_client.DexCallError as exc:
        raise _translate_dex_error(exc, on_404="audience_not_found") from exc
    except dex_client.DexClientError as exc:
        raise _bad_gateway(f"dex_client_error: {exc}") from exc

    return CompositeAudienceResponse(
        reservation=reservation, descriptor=descriptor, count=count,
    )


@router.get("/{reservation_id}/members")
async def get_reservation_members(
    reservation_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(verify_supabase_jwt),
) -> dict[str, Any]:
    row, cols = await _fetch_owned(reservation_id, user.active_organization_id)
    reservation = _row_to_response(row, cols)
    bearer = _bearer_from_request(request)

    try:
        return await dex_client.list_audience_members(
            reservation.data_engine_audience_id,
            limit=limit, offset=offset,
            bearer_token=bearer,
        )
    except dex_client.DexCallError as exc:
        raise _translate_dex_error(exc, on_404="audience_not_found") from exc
    except dex_client.DexClientError as exc:
        raise _bad_gateway(f"dex_client_error: {exc}") from exc


# ────────────────────────────── internals ────────────────────────────


async def _fetch_owned(
    reservation_id: UUID, organization_id: UUID | None,
) -> tuple[tuple, list[str]]:
    if organization_id is None:
        raise _bad_request("organization_required")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.org_audience_reservations
                WHERE id = %s AND organization_id = %s
                """,
                (str(reservation_id), str(organization_id)),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        # 404 (not 403) — don't leak existence across orgs.
        raise _not_found("reservation_not_found")
    return row, cols
