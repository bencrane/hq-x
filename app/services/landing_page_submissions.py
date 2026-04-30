"""Form-submission persistence + validation for hosted landing pages.

`record_submission` validates the incoming form_data against the step's
`landing_page_config.cta.form_schema`, persists the row, and returns
the persisted record. Validation happens here (not at the Pydantic
layer in app/models/landing_page.py) because the schema is per-step
JSONB rather than a shared static type.

`list_submissions_for_*` powers the customer dashboard's leads view
and is also reused by the Slice 5 analytics endpoints.

The service is intentionally permissive about extra fields — we
quarantine them under `_extras` rather than rejecting them, so a stale
form rendered against an updated schema doesn't lose data. The
honeypot check + rate limit live in the router (HTTP-layer concerns).
"""

from __future__ import annotations

import json as _json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection

logger = logging.getLogger(__name__)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TEL_RE = re.compile(r"^[+\d][\d\s().-]{4,}$")
_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


class FormValidationError(Exception):
    """Raised when form_data fails the step's form_schema."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(f"form validation failed: {errors}")
        self.errors = errors


@dataclass(frozen=True)
class SubmissionRecord:
    id: UUID
    organization_id: UUID
    brand_id: UUID
    campaign_id: UUID
    channel_campaign_id: UUID
    channel_campaign_step_id: UUID
    recipient_id: UUID
    form_data: dict[str, Any]
    source_metadata: dict[str, Any] | None
    submitted_at: datetime


def validate_against_schema(
    *, form_data: dict[str, Any], form_schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate raw form_data against a step's form_schema.

    Returns `(clean_data, extras)`:
      * clean_data is the subset that matches schema fields, with
        type-specific normalization (whitespace stripped, etc).
      * extras carries fields not in the schema — never rejected, just
        quarantined under `_extras` in the persisted form_data so the
        operator can recover anything a stale form sent.

    Raises FormValidationError with a per-field map when required
    fields are missing or values fail the type check.
    """
    fields = form_schema.get("fields") or []
    fields_by_name = {f["name"]: f for f in fields if isinstance(f, dict) and "name" in f}

    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}

    for name, field in fields_by_name.items():
        raw = form_data.get(name)
        ftype = field.get("type")
        required = bool(field.get("required", False))

        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            if required:
                errors[name] = "required"
            continue

        if ftype == "email":
            if not _EMAIL_RE.match(str(raw).strip()):
                errors[name] = "invalid email"
                continue
            clean[name] = str(raw).strip().lower()
        elif ftype == "tel":
            v = str(raw).strip()
            if not _TEL_RE.match(v):
                errors[name] = "invalid phone"
                continue
            clean[name] = v
        elif ftype == "url":
            v = str(raw).strip()
            if not _URL_RE.match(v):
                errors[name] = "invalid url"
                continue
            clean[name] = v
        elif ftype == "checkbox":
            # HTML checkboxes only POST when checked. Coerce common truthy values.
            v = str(raw).strip().lower()
            clean[name] = v in ("true", "on", "yes", "1")
        elif ftype == "select":
            opts = field.get("options") or []
            allowed = {o.get("value") for o in opts if isinstance(o, dict)}
            if allowed and raw not in allowed:
                errors[name] = "value not in options"
                continue
            clean[name] = str(raw)
        else:
            # text / textarea / unknown — pass through as trimmed string.
            clean[name] = str(raw).strip()

    if errors:
        raise FormValidationError(errors)

    extras = {k: v for k, v in form_data.items() if k not in fields_by_name}
    return clean, extras


async def record_submission(
    *,
    organization_id: UUID,
    brand_id: UUID,
    campaign_id: UUID,
    channel_campaign_id: UUID,
    channel_campaign_step_id: UUID,
    recipient_id: UUID,
    form_data: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
) -> SubmissionRecord:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.landing_page_submissions (
                    organization_id, brand_id, campaign_id,
                    channel_campaign_id, channel_campaign_step_id,
                    recipient_id, form_data, source_metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                RETURNING
                    id, organization_id, brand_id, campaign_id,
                    channel_campaign_id, channel_campaign_step_id,
                    recipient_id, form_data, source_metadata, submitted_at
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    str(campaign_id),
                    str(channel_campaign_id),
                    str(channel_campaign_step_id),
                    str(recipient_id),
                    _json.dumps(form_data),
                    None if source_metadata is None else _json.dumps(source_metadata),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return SubmissionRecord(
        id=row[0],
        organization_id=row[1],
        brand_id=row[2],
        campaign_id=row[3],
        channel_campaign_id=row[4],
        channel_campaign_step_id=row[5],
        recipient_id=row[6],
        form_data=row[7],
        source_metadata=row[8],
        submitted_at=row[9],
    )


async def list_submissions_for_org(
    *,
    organization_id: UUID,
    brand_id: UUID | None = None,
    channel_campaign_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[SubmissionRecord], int]:
    """Returns (rows, total_count) — total ignores limit/offset for
    paging metadata."""
    where: list[str] = ["organization_id = %s"]
    params: list[Any] = [str(organization_id)]
    if brand_id is not None:
        where.append("brand_id = %s")
        params.append(str(brand_id))
    if channel_campaign_id is not None:
        where.append("channel_campaign_id = %s")
        params.append(str(channel_campaign_id))
    if channel_campaign_step_id is not None:
        where.append("channel_campaign_step_id = %s")
        params.append(str(channel_campaign_step_id))
    if from_date is not None:
        where.append("submitted_at >= %s")
        params.append(from_date)
    if to_date is not None:
        where.append("submitted_at < %s")
        params.append(to_date)

    where_sql = " AND ".join(where)

    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT COUNT(*) FROM business.landing_page_submissions
            WHERE {where_sql}
            """,
            params,
        )
        total_row = await cur.fetchone()
        total = int(total_row[0]) if total_row else 0

        await cur.execute(
            f"""
            SELECT
                id, organization_id, brand_id, campaign_id,
                channel_campaign_id, channel_campaign_step_id,
                recipient_id, form_data, source_metadata, submitted_at
            FROM business.landing_page_submissions
            WHERE {where_sql}
            ORDER BY submitted_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = await cur.fetchall()

    records = [
        SubmissionRecord(
            id=r[0],
            organization_id=r[1],
            brand_id=r[2],
            campaign_id=r[3],
            channel_campaign_id=r[4],
            channel_campaign_step_id=r[5],
            recipient_id=r[6],
            form_data=r[7],
            source_metadata=r[8],
            submitted_at=r[9],
        )
        for r in rows
    ]
    return records, total


__all__ = [
    "FormValidationError",
    "SubmissionRecord",
    "list_submissions_for_org",
    "record_submission",
    "validate_against_schema",
]
