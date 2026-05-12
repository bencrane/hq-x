"""Audience-spec contract substrate REST API (Phase 2 scaffold).

Endpoints under ``/api/v1/audience-specs`` and ``/api/v1/signings``:

  POST /api/v1/audience-specs                       — create draft
  POST /api/v1/audience-specs/{spec_id}/revisions   — new version
  POST /api/v1/audience-specs/{spec_id}/preview     — count + sample
  POST /api/v1/audience-specs/{spec_id}/sign        — freeze + create signing
  GET  /api/v1/audience-specs/{spec_id}/signings    — signing history
  GET  /api/v1/signings/{signing_id}                — signing detail
  GET  /api/v1/signings/{signing_id}/replenishment  — burn-down forecast

Auth: ``require_flexible_auth`` (operator JWT or trigger shared secret),
per the directive's "Auth: require_flexible_auth per existing hq-x
convention." All endpoints emit X-Data-Lineage via LineageMiddleware
because the evaluator records its catalog reads.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.services.audience_spec import evaluator as evalmod
from app.services.audience_spec.models import AudienceSpec

specs_router = APIRouter(
    prefix="/api/v1/audience-specs",
    tags=["audience-specs"],
)
signings_router = APIRouter(
    prefix="/api/v1/signings",
    tags=["audience-specs"],
)


# ─── shared schemas ───────────────────────────────────────────────────


class CreateSpecRequest(BaseModel):
    partner_id: UUID
    content: AudienceSpec
    notes: str | None = None
    model_config = ConfigDict(extra="forbid")


class ReviseSpecRequest(BaseModel):
    content: AudienceSpec
    notes: str | None = None
    model_config = ConfigDict(extra="forbid")


class SignRequest(BaseModel):
    partner_signature: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class SpecResponse(BaseModel):
    spec_id: UUID
    partner_id: UUID
    version: int
    parent_spec_id: UUID | None
    content: dict[str, Any]
    status: str
    required_freshness: list[dict[str, Any]] | None
    created_at: datetime
    created_by_user_id: UUID | None
    notes: str | None


class SigningResponse(BaseModel):
    signing_id: UUID
    spec_id: UUID
    signed_at: datetime
    catalog_snapshot_ts: datetime
    count_at_signing: int
    cohort_manifest_uri: str
    partner_signature: dict[str, Any] | None
    contract_term_days: int
    expires_at: datetime
    source_freshness_at_signing: list[dict[str, Any]] | None
    notes: str | None


class FreshnessCheckSchema(BaseModel):
    source: str
    max_age_seconds: int
    observed_age_seconds: int | None
    ok: bool


class PreviewResponse(BaseModel):
    count: int
    sample: list[dict[str, Any]]
    sources_used: list[str]
    freshness_checks: list[FreshnessCheckSchema]
    snapshot_ts: datetime
    elapsed_s: float


class ReplenishmentResponse(BaseModel):
    signing_id: UUID
    spec_id: UUID
    count_at_signing: int
    live_count: int
    delta: int
    days_remaining: int
    at_risk: bool
    freshness_now: list[dict[str, Any]]


# ─── helpers ──────────────────────────────────────────────────────────


def _user_id(auth: FlexibleContext) -> UUID | None:
    """Pull a user_id from the auth context, or None for system callers."""
    if hasattr(auth, "auth_user_id"):
        return auth.auth_user_id
    return None


def _spec_row_to_response(row: dict[str, Any]) -> SpecResponse:
    return SpecResponse(
        spec_id=row["spec_id"],
        partner_id=row["partner_id"],
        version=row["version"],
        parent_spec_id=row.get("parent_spec_id"),
        content=row["content"],
        status=row["status"],
        required_freshness=row.get("required_freshness"),
        created_at=row["created_at"],
        created_by_user_id=row.get("created_by_user_id"),
        notes=row.get("notes"),
    )


def _sig_row_to_response(row: dict[str, Any]) -> SigningResponse:
    return SigningResponse(
        signing_id=row["signing_id"],
        spec_id=row["spec_id"],
        signed_at=row["signed_at"],
        catalog_snapshot_ts=row["catalog_snapshot_ts"],
        count_at_signing=row["count_at_signing"],
        cohort_manifest_uri=row["cohort_manifest_uri"],
        partner_signature=row.get("partner_signature"),
        contract_term_days=row["contract_term_days"],
        expires_at=row["expires_at"],
        source_freshness_at_signing=row.get("source_freshness_at_signing"),
        notes=row.get("notes"),
    )


def _freshness_breach_to_409(exc: evalmod.FreshnessSLABreach) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "freshness_sla_breach",
            "message": str(exc),
            "checks": [
                {
                    "source": c.source,
                    "max_age_seconds": c.max_age_seconds,
                    "observed_age_seconds": c.observed_age_seconds,
                    "ok": c.ok,
                }
                for c in exc.checks
            ],
        },
    )


# ─── spec endpoints ──────────────────────────────────────────────────


@specs_router.post(
    "",
    response_model=SpecResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_spec(
    body: CreateSpecRequest,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> SpecResponse:
    """Create a draft audience spec for a partner."""
    spec_id = uuid4()
    user_id = _user_id(auth)
    required_freshness = [
        {"source": r.source, "max_age_seconds": r.max_age_seconds}
        for r in body.content.required_freshness
    ] or None

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.audience_specs (
                    spec_id, partner_id, version, parent_spec_id,
                    content, status, required_freshness,
                    created_by_user_id, notes
                ) VALUES (
                    %s, %s, 1, NULL, %s::jsonb, 'draft', %s::jsonb, %s, %s
                )
                RETURNING spec_id, partner_id, version, parent_spec_id,
                          content, status, required_freshness,
                          created_at, created_by_user_id, notes
                """,
                (
                    str(spec_id),
                    str(body.partner_id),
                    body.content.model_dump_json(),
                    json.dumps(required_freshness) if required_freshness else None,
                    str(user_id) if user_id else None,
                    body.notes,
                ),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]
        await conn.commit()
    return _spec_row_to_response(dict(zip(cols, row, strict=True)))


@specs_router.post(
    "/{spec_id}/revisions",
    response_model=SpecResponse,
    status_code=status.HTTP_201_CREATED,
)
async def revise_spec(
    spec_id: UUID,
    body: ReviseSpecRequest,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> SpecResponse:
    """Create a new revision of an existing spec.

    Marks the parent ``superseded`` and inserts the new version with
    ``version = parent.version + 1`` and ``parent_spec_id = parent.spec_id``.
    """
    user_id = _user_id(auth)
    new_id = uuid4()

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT partner_id, version, status
                FROM business.audience_specs
                WHERE spec_id = %s
                """,
                (str(spec_id),),
            )
            parent = await cur.fetchone()
            if parent is None:
                raise HTTPException(404, detail={"error": "spec_not_found"})
            partner_id, parent_version, parent_status = parent
            if parent_status not in ("draft", "preview", "signed"):
                raise HTTPException(
                    409,
                    detail={
                        "error": "spec_not_revisable",
                        "current_status": parent_status,
                    },
                )

            required_freshness = [
                {"source": r.source, "max_age_seconds": r.max_age_seconds}
                for r in body.content.required_freshness
            ] or None

            await cur.execute(
                """
                INSERT INTO business.audience_specs (
                    spec_id, partner_id, version, parent_spec_id,
                    content, status, required_freshness,
                    created_by_user_id, notes
                ) VALUES (
                    %s, %s, %s, %s, %s::jsonb, 'draft', %s::jsonb, %s, %s
                )
                RETURNING spec_id, partner_id, version, parent_spec_id,
                          content, status, required_freshness,
                          created_at, created_by_user_id, notes
                """,
                (
                    str(new_id),
                    str(partner_id),
                    parent_version + 1,
                    str(spec_id),
                    body.content.model_dump_json(),
                    json.dumps(required_freshness) if required_freshness else None,
                    str(user_id) if user_id else None,
                    body.notes,
                ),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]

            # Demote parent so we don't have two active drafts under one chain.
            await cur.execute(
                """
                UPDATE business.audience_specs
                SET status = 'superseded'
                WHERE spec_id = %s AND status IN ('draft', 'preview')
                """,
                (str(spec_id),),
            )
        await conn.commit()
    return _spec_row_to_response(dict(zip(cols, row, strict=True)))


@specs_router.post(
    "/{spec_id}/preview",
    response_model=PreviewResponse,
)
async def preview_spec(
    spec_id: UUID,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> PreviewResponse:
    """Run the spec against fresh catalog. Returns count + sample.

    409 if any declared freshness SLA isn't currently met.
    """
    try:
        result = await evalmod.preview(spec_id)
    except evalmod.SpecNotFound:
        raise HTTPException(404, detail={"error": "spec_not_found"})
    except evalmod.FreshnessSLABreach as e:
        raise _freshness_breach_to_409(e)
    except NotImplementedError as e:
        raise HTTPException(
            501,
            detail={"error": "not_implemented", "message": str(e)},
        )

    # Bump status to 'preview' so the lifecycle is visible.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.audience_specs
                SET status = 'preview'
                WHERE spec_id = %s AND status = 'draft'
                """,
                (str(spec_id),),
            )
        await conn.commit()

    return PreviewResponse(
        count=result.count,
        sample=result.sample,
        sources_used=result.sources_used,
        freshness_checks=[
            FreshnessCheckSchema(
                source=c.source,
                max_age_seconds=c.max_age_seconds,
                observed_age_seconds=c.observed_age_seconds,
                ok=c.ok,
            )
            for c in result.freshness_checks
        ],
        snapshot_ts=result.snapshot_ts,
        elapsed_s=result.elapsed_s,
    )


@specs_router.post(
    "/{spec_id}/sign",
    response_model=SigningResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sign_spec(
    spec_id: UUID,
    body: SignRequest,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> SigningResponse:
    """Atomically: freeze cohort manifest to R2, insert signing row.

    Once signed, the spec status is ``signed`` and the manifest is
    immutable. Refund/replenishment math anchors here.
    """
    try:
        signing = await evalmod.sign(spec_id, body.partner_signature)
    except evalmod.SpecNotFound:
        raise HTTPException(404, detail={"error": "spec_not_found"})
    except evalmod.FreshnessSLABreach as e:
        raise _freshness_breach_to_409(e)
    except NotImplementedError as e:
        raise HTTPException(
            501,
            detail={"error": "not_implemented", "message": str(e)},
        )
    return SigningResponse(
        signing_id=signing.signing_id,
        spec_id=signing.spec_id,
        signed_at=signing.signed_at,
        catalog_snapshot_ts=signing.catalog_snapshot_ts,
        count_at_signing=signing.count_at_signing,
        cohort_manifest_uri=signing.cohort_manifest_uri,
        partner_signature=body.partner_signature,
        contract_term_days=signing.contract_term_days,
        expires_at=signing.expires_at,
        source_freshness_at_signing=signing.source_freshness_at_signing,
        notes=None,
    )


@specs_router.get(
    "/{spec_id}/signings",
    response_model=list[SigningResponse],
)
async def list_signings_for_spec(
    spec_id: UUID,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[SigningResponse]:
    """Signing history for one spec."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signing_id, spec_id, signed_at, catalog_snapshot_ts,
                       count_at_signing, cohort_manifest_uri,
                       partner_signature, contract_term_days, expires_at,
                       source_freshness_at_signing, notes
                FROM business.audience_spec_signings
                WHERE spec_id = %s
                ORDER BY signed_at DESC
                """,
                (str(spec_id),),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [
        _sig_row_to_response(dict(zip(cols, r, strict=True)))
        for r in rows
    ]


# ─── signing endpoints ───────────────────────────────────────────────


@signings_router.get(
    "/{signing_id}",
    response_model=SigningResponse,
)
async def get_signing(
    signing_id: UUID,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> SigningResponse:
    """Signing detail with cohort manifest URI."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signing_id, spec_id, signed_at, catalog_snapshot_ts,
                       count_at_signing, cohort_manifest_uri,
                       partner_signature, contract_term_days, expires_at,
                       source_freshness_at_signing, notes
                FROM business.audience_spec_signings
                WHERE signing_id = %s
                """,
                (str(signing_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(404, detail={"error": "signing_not_found"})
            cols = [d[0] for d in cur.description]
    return _sig_row_to_response(dict(zip(cols, row, strict=True)))


@signings_router.get(
    "/{signing_id}/replenishment",
    response_model=ReplenishmentResponse,
)
async def get_replenishment(
    signing_id: UUID,
    auth: FlexibleContext = Depends(require_flexible_auth),
) -> ReplenishmentResponse:
    """Burn-down forecast: live cohort vs at-signing baseline + days remaining."""
    try:
        rep = await evalmod.replenishment_status(signing_id)
    except evalmod.SigningNotFound:
        raise HTTPException(404, detail={"error": "signing_not_found"})
    except NotImplementedError as e:
        raise HTTPException(
            501,
            detail={"error": "not_implemented", "message": str(e)},
        )
    return ReplenishmentResponse(
        signing_id=rep.signing_id,
        spec_id=rep.spec_id,
        count_at_signing=rep.count_at_signing,
        live_count=rep.live_count,
        delta=rep.delta,
        days_remaining=rep.days_remaining,
        at_risk=rep.at_risk,
        freshness_now=rep.freshness_now,
    )
