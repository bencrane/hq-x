"""CRUD + lightweight validation for business.org_doctrine.

Doctrine carries two payloads per org:
  * `doctrine_markdown` — the prose policy doc; passed verbatim into
    subagent user messages where doctrine adherence matters.
  * `parameters` — structured numeric overrides that
    `gtm-sequence-definer` reads to make economics decisions
    (margin floor, outlay cap, per-piece guardrails, default touch
    counts, model tier per step, gating mode default).

The frontend doctrine editor calls these for read+write. The
gtm_pipeline service reads the doctrine at run start for any subagent
whose system prompt references doctrine values.

Validation on `parameters` is intentionally permissive: we coerce
floats / ints, surface obvious type mismatches, and leave
business-logic validation (e.g. "is 0.50 a sane margin floor?") to
the operator. The shape mirrors the JSONB block in
data/orgs/acq-eng/doctrine.md but we don't enforce membership of
specific keys — the operator can extend `parameters` over time.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection

logger = logging.getLogger(__name__)


class OrgDoctrineError(Exception):
    pass


class DoctrineValidationError(OrgDoctrineError):
    pass


_KNOWN_NUMERIC_KEYS = {
    "target_margin_pct",
    "soft_margin_pct",
    "max_capital_outlay_pct_of_revenue",
}
_KNOWN_INT_KEYS = {
    "min_per_piece_cents",
    "max_per_piece_cents",
}
_KNOWN_OBJECT_KEYS = {
    "default_touch_count_by_audience_size_bucket",
    "model_tier_by_step_type",
}
_KNOWN_STRING_KEYS = {"gating_mode_default"}


def validate_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Type-coerce known keys, raise on shape violations.

    Unknown keys pass through unchanged — the operator can add new
    fields without redeploying. Returns the validated copy.
    """
    if not isinstance(parameters, dict):
        raise DoctrineValidationError(
            f"parameters must be an object, got {type(parameters).__name__}"
        )
    out: dict[str, Any] = {}
    for key, value in parameters.items():
        if key in _KNOWN_NUMERIC_KEYS:
            try:
                out[key] = float(value)
            except (TypeError, ValueError) as exc:
                raise DoctrineValidationError(
                    f"parameters.{key} must be numeric, got {value!r}"
                ) from exc
        elif key in _KNOWN_INT_KEYS:
            try:
                out[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise DoctrineValidationError(
                    f"parameters.{key} must be integer, got {value!r}"
                ) from exc
        elif key in _KNOWN_OBJECT_KEYS:
            if not isinstance(value, dict):
                raise DoctrineValidationError(
                    f"parameters.{key} must be an object, got {type(value).__name__}"
                )
            out[key] = value
        elif key in _KNOWN_STRING_KEYS:
            if not isinstance(value, str):
                raise DoctrineValidationError(
                    f"parameters.{key} must be a string, got {type(value).__name__}"
                )
            out[key] = value
        else:
            out[key] = value
    return out


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "organization_id": row[0],
        "doctrine_markdown": row[1],
        "parameters": row[2] or {},
        "updated_at": row[3],
        "updated_by_user_id": row[4],
    }


async def get_for_org(organization_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT organization_id, doctrine_markdown, parameters,
                       updated_at, updated_by_user_id
                FROM business.org_doctrine
                WHERE organization_id = %s
                """,
                (str(organization_id),),
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def upsert(
    *,
    organization_id: UUID,
    doctrine_markdown: str,
    parameters: dict[str, Any],
    updated_by_user_id: UUID | None = None,
) -> dict[str, Any]:
    """Upsert the doctrine row. Validates `parameters` first; raises
    DoctrineValidationError on shape violation."""
    validated = validate_parameters(parameters)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.org_doctrine (
                    organization_id, doctrine_markdown, parameters,
                    updated_by_user_id
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (organization_id) DO UPDATE
                SET doctrine_markdown = EXCLUDED.doctrine_markdown,
                    parameters = EXCLUDED.parameters,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = NOW()
                RETURNING organization_id, doctrine_markdown, parameters,
                          updated_at, updated_by_user_id
                """,
                (
                    str(organization_id),
                    doctrine_markdown,
                    Jsonb(validated),
                    str(updated_by_user_id) if updated_by_user_id else None,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return _row_to_dict(row)


__all__ = [
    "OrgDoctrineError",
    "DoctrineValidationError",
    "validate_parameters",
    "get_for_org",
    "upsert",
]
