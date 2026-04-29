"""DB read/write for dmaas_scaffolds, dmaas_designs, dmaas_scaffold_authoring_sessions.

Thin layer over psycopg. JSONB columns round-trip via psycopg.types.json.Jsonb.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection

# ---------------------------------------------------------------------------
# Scaffolds
# ---------------------------------------------------------------------------


@dataclass
class Scaffold:
    id: UUID
    slug: str
    name: str
    description: str | None
    format: str
    compatible_specs: list[dict[str, str]]
    prop_schema: dict[str, Any]
    constraint_specification: dict[str, Any]
    preview_image_url: str | None
    vertical_tags: list[str]
    is_active: bool
    version_number: int
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


_SCAFFOLD_COLS = (
    "id, slug, name, description, format, "
    "compatible_specs, prop_schema, constraint_specification, "
    "preview_image_url, vertical_tags, is_active, version_number, "
    "created_by_user_id, created_at, updated_at"
)


def _row_to_scaffold(row: tuple) -> Scaffold:
    return Scaffold(
        id=row[0],
        slug=row[1],
        name=row[2],
        description=row[3],
        format=row[4],
        compatible_specs=row[5] or [],
        prop_schema=row[6] or {},
        constraint_specification=row[7],
        preview_image_url=row[8],
        vertical_tags=list(row[9] or []),
        is_active=row[10],
        version_number=row[11],
        created_by_user_id=row[12],
        created_at=row[13],
        updated_at=row[14],
    )


async def list_scaffolds(
    *,
    format: str | None = None,
    vertical: str | None = None,
    spec_category: str | None = None,
    active_only: bool = True,
) -> list[Scaffold]:
    where = []
    params: list[Any] = []
    if active_only:
        where.append("is_active")
    if format is not None:
        where.append("format = %s")
        params.append(format)
    if vertical is not None:
        where.append("%s = ANY(vertical_tags)")
        params.append(vertical)
    if spec_category is not None:
        # compatible_specs is jsonb array of {category, variant}; match any.
        where.append("compatible_specs @> %s::jsonb")
        params.append(f'[{{"category": "{spec_category}"}}]')
    sql = f"SELECT {_SCAFFOLD_COLS} FROM dmaas_scaffolds"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY format, slug, version_number DESC"
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [_row_to_scaffold(r) for r in rows]


async def get_scaffold_by_slug(slug: str) -> Scaffold | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SCAFFOLD_COLS} FROM dmaas_scaffolds WHERE slug = %s "
            "ORDER BY version_number DESC LIMIT 1",
            (slug,),
        )
        row = await cur.fetchone()
    return _row_to_scaffold(row) if row else None


async def get_scaffold_by_id(scaffold_id: UUID) -> Scaffold | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SCAFFOLD_COLS} FROM dmaas_scaffolds WHERE id = %s",
            (str(scaffold_id),),
        )
        row = await cur.fetchone()
    return _row_to_scaffold(row) if row else None


async def insert_scaffold(
    *,
    slug: str,
    name: str,
    description: str | None,
    format: str,
    compatible_specs: list[dict[str, str]],
    prop_schema: dict[str, Any],
    constraint_specification: dict[str, Any],
    preview_image_url: str | None,
    vertical_tags: list[str],
    is_active: bool,
    version_number: int,
    created_by_user_id: UUID | None,
) -> Scaffold:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dmaas_scaffolds "
            f"(slug, name, description, format, compatible_specs, prop_schema, "
            f"constraint_specification, preview_image_url, vertical_tags, "
            f"is_active, version_number, created_by_user_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_SCAFFOLD_COLS}",
            (
                slug,
                name,
                description,
                format,
                Jsonb(compatible_specs),
                Jsonb(prop_schema),
                Jsonb(constraint_specification),
                preview_image_url,
                vertical_tags,
                is_active,
                version_number,
                str(created_by_user_id) if created_by_user_id else None,
            ),
        )
        row = await cur.fetchone()
    return _row_to_scaffold(row)


async def update_scaffold(
    *,
    slug: str,
    fields: dict[str, Any],
) -> Scaffold | None:
    """Patch fields by slug. Latest version_number is updated; earlier
    versions are immutable history."""
    if not fields:
        return await get_scaffold_by_slug(slug)
    sets = []
    params: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        if k in ("compatible_specs", "prop_schema", "constraint_specification"):
            params.append(Jsonb(v))
        else:
            params.append(v)
    sets.append("updated_at = NOW()")
    params.append(slug)
    sql = (
        f"UPDATE dmaas_scaffolds SET {', '.join(sets)} "
        f"WHERE id = (SELECT id FROM dmaas_scaffolds WHERE slug = %s "
        f"ORDER BY version_number DESC LIMIT 1) "
        f"RETURNING {_SCAFFOLD_COLS}"
    )
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
    return _row_to_scaffold(row) if row else None


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@dataclass
class Design:
    id: UUID
    scaffold_id: UUID
    spec_category: str
    spec_variant: str
    content_config: dict[str, Any]
    resolved_positions: dict[str, Any]
    brand_id: UUID | None
    audience_template_id: UUID | None
    created_by_user_id: UUID | None
    version_number: int
    created_at: datetime
    updated_at: datetime


_DESIGN_COLS = (
    "id, scaffold_id, spec_category, spec_variant, content_config, "
    "resolved_positions, brand_id, audience_template_id, created_by_user_id, "
    "version_number, created_at, updated_at"
)


def _row_to_design(row: tuple) -> Design:
    return Design(
        id=row[0],
        scaffold_id=row[1],
        spec_category=row[2],
        spec_variant=row[3],
        content_config=row[4] or {},
        resolved_positions=row[5] or {},
        brand_id=row[6],
        audience_template_id=row[7],
        created_by_user_id=row[8],
        version_number=row[9],
        created_at=row[10],
        updated_at=row[11],
    )


async def insert_design(
    *,
    scaffold_id: UUID,
    spec_category: str,
    spec_variant: str,
    content_config: dict[str, Any],
    resolved_positions: dict[str, Any],
    brand_id: UUID | None,
    audience_template_id: UUID | None,
    created_by_user_id: UUID | None,
) -> Design:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dmaas_designs "
            f"(scaffold_id, spec_category, spec_variant, content_config, "
            f"resolved_positions, brand_id, audience_template_id, created_by_user_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_DESIGN_COLS}",
            (
                str(scaffold_id),
                spec_category,
                spec_variant,
                Jsonb(content_config),
                Jsonb(resolved_positions),
                str(brand_id) if brand_id else None,
                str(audience_template_id) if audience_template_id else None,
                str(created_by_user_id) if created_by_user_id else None,
            ),
        )
        row = await cur.fetchone()
    return _row_to_design(row)


async def get_design(design_id: UUID) -> Design | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_DESIGN_COLS} FROM dmaas_designs WHERE id = %s",
            (str(design_id),),
        )
        row = await cur.fetchone()
    return _row_to_design(row) if row else None


async def update_design_content(
    *,
    design_id: UUID,
    content_config: dict[str, Any],
    resolved_positions: dict[str, Any],
) -> Design | None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE dmaas_designs SET content_config = %s, resolved_positions = %s, "
            f"version_number = version_number + 1, updated_at = NOW() "
            f"WHERE id = %s RETURNING {_DESIGN_COLS}",
            (Jsonb(content_config), Jsonb(resolved_positions), str(design_id)),
        )
        row = await cur.fetchone()
    return _row_to_design(row) if row else None


async def list_designs(
    *,
    brand_id: UUID | None = None,
    audience_template_id: UUID | None = None,
    scaffold_id: UUID | None = None,
    limit: int = 50,
) -> list[Design]:
    where = []
    params: list[Any] = []
    if brand_id is not None:
        where.append("brand_id = %s")
        params.append(str(brand_id))
    if audience_template_id is not None:
        where.append("audience_template_id = %s")
        params.append(str(audience_template_id))
    if scaffold_id is not None:
        where.append("scaffold_id = %s")
        params.append(str(scaffold_id))
    sql = f"SELECT {_DESIGN_COLS} FROM dmaas_designs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [_row_to_design(r) for r in rows]


# ---------------------------------------------------------------------------
# Authoring sessions
# ---------------------------------------------------------------------------


@dataclass
class AuthoringSession:
    id: UUID
    scaffold_id: UUID | None
    prompt: str
    proposed_constraint_specification: dict[str, Any]
    accepted: bool
    notes: str | None
    created_by_user_id: UUID | None
    created_at: datetime


_AUTHORING_COLS = (
    "id, scaffold_id, prompt, proposed_constraint_specification, accepted, "
    "notes, created_by_user_id, created_at"
)


def _row_to_authoring(row: tuple) -> AuthoringSession:
    return AuthoringSession(
        id=row[0],
        scaffold_id=row[1],
        prompt=row[2],
        proposed_constraint_specification=row[3],
        accepted=row[4],
        notes=row[5],
        created_by_user_id=row[6],
        created_at=row[7],
    )


async def insert_authoring_session(
    *,
    scaffold_id: UUID | None,
    prompt: str,
    proposed_constraint_specification: dict[str, Any],
    accepted: bool,
    notes: str | None,
    created_by_user_id: UUID | None,
) -> AuthoringSession:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO dmaas_scaffold_authoring_sessions "
            f"(scaffold_id, prompt, proposed_constraint_specification, accepted, notes, created_by_user_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s) "
            f"RETURNING {_AUTHORING_COLS}",
            (
                str(scaffold_id) if scaffold_id else None,
                prompt,
                Jsonb(proposed_constraint_specification),
                accepted,
                notes,
                str(created_by_user_id) if created_by_user_id else None,
            ),
        )
        row = await cur.fetchone()
    return _row_to_authoring(row)


async def list_authoring_sessions(*, limit: int = 50) -> list[AuthoringSession]:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_AUTHORING_COLS} FROM dmaas_scaffold_authoring_sessions "
            f"ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [_row_to_authoring(r) for r in rows]
