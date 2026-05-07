"""Outreach model emails — operator-curated reference outreach copy.

These are the operator's real outreach exemplars that downstream agents
read as voice/style anchors. The per-recipient creative bundle pulls
2-3 best-matching rows by purpose + audience_template_slug + step_index
when generating per-member outreach so the operator's voice survives
the LLM call by demonstration, not description.

Selectors (in match-priority order):
  1. organization_id (always)
  2. purpose (always)
  3. audience_template_slug (preferred match; fallback to NULL-template
     rows if no slug-specific row exists)
  4. step_index (preferred match; fallback to NULL-step rows)
  5. is_active = TRUE (always)

Order: most-specific match first, then by recency.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db import get_db_connection

_PURPOSES = (
    "demand_side_outreach",
    "supply_side_opt_in",
    "lead_intro",
    "general",
)


class OutreachModelEmailValidationError(Exception):
    pass


def _validate_purpose(value: str) -> None:
    if value not in _PURPOSES:
        raise OutreachModelEmailValidationError(
            f"purpose must be one of {_PURPOSES}, got {value!r}"
        )


async def list_for_bundle(
    *,
    organization_id: UUID,
    purpose: str,
    audience_template_slug: str | None,
    step_index: int | None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Read-side selector for the per-recipient creative bundle.

    Ranks rows by match specificity:
      * exact (slug + step) > slug-only > step-only > generic-fallback
    Generic-fallback rows have NULL audience_template_slug and NULL
    step_index — they're the operator's "voice anywhere" entries.
    """
    _validate_purpose(purpose)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, label, subject, body, notes,
                       audience_template_slug, step_index,
                       metadata, created_at
                FROM business.outreach_model_emails
                WHERE organization_id = %s
                  AND purpose = %s
                  AND is_active = TRUE
                  AND (audience_template_slug = %s OR audience_template_slug IS NULL)
                  AND (step_index = %s OR step_index IS NULL)
                ORDER BY
                    -- specificity: exact slug+step rank highest
                    (audience_template_slug IS NOT NULL)::int +
                    (step_index IS NOT NULL)::int DESC,
                    created_at DESC
                LIMIT %s
                """,
                (
                    str(organization_id),
                    purpose,
                    audience_template_slug,
                    step_index,
                    max(1, min(limit, 10)),
                ),
            )
            rows = await cur.fetchall()
    keys = [
        "id", "label", "subject", "body", "notes",
        "audience_template_slug", "step_index", "metadata", "created_at",
    ]
    return [dict(zip(keys, r, strict=True)) for r in rows]


async def list_all(
    *,
    organization_id: UUID,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["organization_id = %s"]
    args: list[Any] = [str(organization_id)]
    if not include_inactive:
        where.append("is_active = TRUE")
    args.extend([min(max(limit, 1), 200), max(offset, 0)])
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, organization_id, brand_id, purpose,
                       audience_template_slug, step_index,
                       label, subject, body, notes, is_active,
                       metadata, created_at, updated_at, created_by_user_id
                FROM business.outreach_model_emails
                WHERE {' AND '.join(where)}
                ORDER BY purpose, audience_template_slug NULLS LAST,
                         step_index NULLS LAST, created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    keys = [
        "id", "organization_id", "brand_id", "purpose",
        "audience_template_slug", "step_index",
        "label", "subject", "body", "notes", "is_active",
        "metadata", "created_at", "updated_at", "created_by_user_id",
    ]
    return [dict(zip(keys, r, strict=True)) for r in rows]


async def create(
    *,
    organization_id: UUID,
    brand_id: UUID | None,
    purpose: str,
    audience_template_slug: str | None,
    step_index: int | None,
    label: str,
    subject: str,
    body: str,
    notes: str | None,
    metadata: dict[str, Any] | None = None,
    created_by_user_id: UUID | None = None,
) -> dict[str, Any]:
    _validate_purpose(purpose)
    if not label.strip():
        raise OutreachModelEmailValidationError("label required")
    if not subject.strip():
        raise OutreachModelEmailValidationError("subject required")
    if not body.strip():
        raise OutreachModelEmailValidationError("body required")
    if step_index is not None and step_index <= 0:
        raise OutreachModelEmailValidationError("step_index must be positive")

    import json
    md_json = json.dumps(metadata or {}, default=str)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.outreach_model_emails (
                    organization_id, brand_id, purpose,
                    audience_template_slug, step_index,
                    label, subject, body, notes,
                    metadata, created_by_user_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (
                    str(organization_id),
                    str(brand_id) if brand_id else None,
                    purpose,
                    audience_template_slug,
                    step_index,
                    label.strip(),
                    subject,
                    body,
                    notes,
                    md_json,
                    str(created_by_user_id) if created_by_user_id else None,
                ),
            )
            new_id = (await cur.fetchone())[0]
        await conn.commit()
    return await get(new_id)


async def get(model_email_id: UUID | str) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, organization_id, brand_id, purpose,
                       audience_template_slug, step_index,
                       label, subject, body, notes, is_active,
                       metadata, created_at, updated_at, created_by_user_id
                FROM business.outreach_model_emails
                WHERE id = %s
                """,
                (str(model_email_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise OutreachModelEmailValidationError(
            f"outreach_model_email {model_email_id} not found"
        )
    keys = [
        "id", "organization_id", "brand_id", "purpose",
        "audience_template_slug", "step_index",
        "label", "subject", "body", "notes", "is_active",
        "metadata", "created_at", "updated_at", "created_by_user_id",
    ]
    return dict(zip(keys, row, strict=True))


async def update(
    *,
    model_email_id: UUID | str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Partial update. Only whitelisted columns may be set."""
    allowed = {
        "purpose", "audience_template_slug", "step_index",
        "label", "subject", "body", "notes", "is_active",
        "brand_id",
    }
    sets: list[str] = []
    args: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "purpose":
            _validate_purpose(value)
        sets.append(f"{key} = %s")
        args.append(value if key != "brand_id" else (str(value) if value else None))
    if not sets:
        return await get(model_email_id)
    sets.append("updated_at = NOW()")
    args.append(str(model_email_id))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.outreach_model_emails
                SET {', '.join(sets)}
                WHERE id = %s
                """,
                args,
            )
        await conn.commit()
    return await get(model_email_id)


async def delete(model_email_id: UUID | str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM business.outreach_model_emails WHERE id = %s",
                (str(model_email_id),),
            )
        await conn.commit()


__all__ = [
    "OutreachModelEmailValidationError",
    "list_for_bundle",
    "list_all",
    "create",
    "get",
    "update",
    "delete",
]
