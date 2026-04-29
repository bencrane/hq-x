"""Audience drafts REST API.

User-owned saved customizations of a DEX (data-engine-x) audience template.
HQ-X is the source of truth for drafts; DEX owns the template catalog and
live preview/query surface. HQ-X never validates filter contents — that's
DEX's job at query time and the frontend's job pre-save.

Scope: basic CRUD (list mine, create, get, update, delete). No proposal
lifecycle, no lead/company association, no payment hook.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.db import get_db_connection

router = APIRouter(prefix="/api/audience-drafts", tags=["audience-drafts"])


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


# ────────────────────────────── schemas ──────────────────────────────


class _DraftBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    audience_template_slug: str = Field(..., min_length=1, max_length=120)
    source_endpoint: str = Field(..., min_length=1)
    filter_overrides: dict[str, Any] = Field(default_factory=dict)
    resolved_filters: dict[str, Any]
    last_preview_total_matched: int | None = Field(default=None, ge=0)
    last_preview_at: datetime | None = None
    model_config = {"extra": "forbid"}

    @field_validator("audience_template_slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("audience_template_slug must match ^[a-z0-9-]+$")
        return v

    @field_validator("source_endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        if not v.startswith("/api/v1/"):
            raise ValueError("source_endpoint must start with /api/v1/")
        return v


class CreateDraftRequest(_DraftBase):
    pass


class UpdateDraftRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    filter_overrides: dict[str, Any] | None = None
    resolved_filters: dict[str, Any] | None = None
    last_preview_total_matched: int | None = Field(default=None, ge=0)
    last_preview_at: datetime | None = None
    model_config = {"extra": "forbid"}


class DraftResponse(BaseModel):
    id: UUID
    created_by_user_id: UUID
    name: str
    audience_template_slug: str
    source_endpoint: str
    filter_overrides: dict[str, Any]
    resolved_filters: dict[str, Any]
    last_preview_total_matched: int | None
    last_preview_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ────────────────────────────── helpers ──────────────────────────────


_COLUMNS = """
    id, created_by_user_id, name, audience_template_slug, source_endpoint,
    filter_overrides, resolved_filters,
    last_preview_total_matched, last_preview_at,
    created_at, updated_at
"""


def _row_to_response(row: tuple, cols: list[str]) -> DraftResponse:
    return DraftResponse(**dict(zip(cols, row, strict=True)))


# ────────────────────────────── endpoints ────────────────────────────


@router.post("", response_model=DraftResponse, status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: CreateDraftRequest,
    user: UserContext = Depends(verify_supabase_jwt),
) -> DraftResponse:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.audience_drafts (
                    created_by_user_id, name, audience_template_slug,
                    source_endpoint, filter_overrides, resolved_filters,
                    last_preview_total_matched, last_preview_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    str(user.auth_user_id),
                    body.name,
                    body.audience_template_slug,
                    body.source_endpoint,
                    _json(body.filter_overrides),
                    _json(body.resolved_filters),
                    body.last_preview_total_matched,
                    body.last_preview_at,
                ),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]
        await conn.commit()
    return _row_to_response(row, cols)


@router.get("", response_model=list[DraftResponse])
async def list_drafts(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(verify_supabase_jwt),
) -> list[DraftResponse]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.audience_drafts
                WHERE created_by_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (str(user.auth_user_id), limit, offset),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [_row_to_response(r, cols) for r in rows]


@router.get("/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: UUID,
    user: UserContext = Depends(verify_supabase_jwt),
) -> DraftResponse:
    row, cols = await _fetch_owned(draft_id, user.auth_user_id)
    return _row_to_response(row, cols)


@router.patch("/{draft_id}", response_model=DraftResponse)
async def update_draft(
    draft_id: UUID,
    body: UpdateDraftRequest,
    user: UserContext = Depends(verify_supabase_jwt),
) -> DraftResponse:
    fields: list[str] = []
    params: list[Any] = []

    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        fields.append("name = %s")
        params.append(data["name"])
    if "filter_overrides" in data:
        fields.append("filter_overrides = %s::jsonb")
        params.append(_json(data["filter_overrides"]))
    if "resolved_filters" in data:
        fields.append("resolved_filters = %s::jsonb")
        params.append(_json(data["resolved_filters"]))
    if "last_preview_total_matched" in data:
        fields.append("last_preview_total_matched = %s")
        params.append(data["last_preview_total_matched"])
    if "last_preview_at" in data:
        fields.append("last_preview_at = %s")
        params.append(data["last_preview_at"])

    if not fields:
        # Nothing to update — just return current state (after ownership check).
        row, cols = await _fetch_owned(draft_id, user.auth_user_id)
        return _row_to_response(row, cols)

    fields.append("updated_at = NOW()")
    params.extend([str(draft_id), str(user.auth_user_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.audience_drafts
                SET {", ".join(fields)}
                WHERE id = %s AND created_by_user_id = %s
                RETURNING {_COLUMNS}
                """,
                tuple(params),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "draft_not_found"})
    return _row_to_response(row, cols)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    draft_id: UUID,
    user: UserContext = Depends(verify_supabase_jwt),
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM business.audience_drafts
                WHERE id = %s AND created_by_user_id = %s
                """,
                (str(draft_id), str(user.auth_user_id)),
            )
            deleted = cur.rowcount
        await conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "draft_not_found"})


# ────────────────────────────── internals ────────────────────────────


async def _fetch_owned(draft_id: UUID, owner: UUID) -> tuple[tuple, list[str]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.audience_drafts
                WHERE id = %s AND created_by_user_id = %s
                """,
                (str(draft_id), str(owner)),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        # 404 (not 403) — don't leak existence of other users' drafts.
        raise HTTPException(status_code=404, detail={"error": "draft_not_found"})
    return row, cols


def _json(value: Any) -> str:
    import json
    return json.dumps(value)
