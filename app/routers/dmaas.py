"""DMaaS REST router: scaffolds, designs, authoring sessions, validation.

Mounted at `/api/v1/dmaas/*`. Auth model:
  * Scaffold writes + authoring sessions: operator-only.
  * Scaffold reads + design CRUD: any authenticated user.

The MCP wrapper in app/mcp/dmaas.py exposes the same surface as MCP tools."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.dmaas import repository as repo
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.repository import Design, Scaffold
from app.dmaas.service import (
    bind_spec_zones,
    derive_intrinsics_from_content,
    resolve_spec_binding,
    run_solve,
    validate_content_against_prop_schema,
)
from app.dmaas.solver import solve
from app.models.dmaas import (
    AuthoringSessionCreateRequest,
    AuthoringSessionListResponse,
    AuthoringSessionResponse,
    DesignCreateRequest,
    DesignListResponse,
    DesignResponse,
    DesignUpdateRequest,
    PreviewRequest,
    ScaffoldCreateRequest,
    ScaffoldListResponse,
    ScaffoldResponse,
    ScaffoldUpdateRequest,
    SolveResultResponse,
    ValidateConstraintsRequest,
)
from app.observability import incr_metric

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dmaas", tags=["dmaas"])


# ---------------------------------------------------------------------------
# Marshalling helpers
# ---------------------------------------------------------------------------


def _scaffold_to_response(s: Scaffold) -> ScaffoldResponse:
    return ScaffoldResponse(
        id=s.id,
        slug=s.slug,
        name=s.name,
        description=s.description,
        format=s.format,  # type: ignore[arg-type]
        compatible_specs=s.compatible_specs,  # type: ignore[arg-type]
        prop_schema=s.prop_schema,
        constraint_specification=s.constraint_specification,
        preview_image_url=s.preview_image_url,
        vertical_tags=s.vertical_tags,
        is_active=s.is_active,
        version_number=s.version_number,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


def _design_to_response(d: Design) -> DesignResponse:
    return DesignResponse(
        id=d.id,
        scaffold_id=d.scaffold_id,
        spec_category=d.spec_category,
        spec_variant=d.spec_variant,
        content_config=d.content_config,
        resolved_positions=d.resolved_positions,
        brand_id=d.brand_id,
        audience_template_id=d.audience_template_id,
        version_number=d.version_number,
        created_at=d.created_at.isoformat(),
        updated_at=d.updated_at.isoformat(),
    )


def _solve_result_to_response(
    result, binding=None
) -> SolveResultResponse:
    payload = SolveResultResponse(
        is_valid=result.is_valid,
        positions=result.positions_dict(),
        conflicts=[
            {
                "constraint_index": c.constraint_index,
                "constraint_type": c.constraint_type,
                "phase": c.phase,
                "message": c.message,
                "detail": c.detail,
            }
            for c in result.conflicts
        ],
    )
    if binding is not None:
        payload.canvas = binding.canvas.to_dict()
        payload.zones = {n: r.to_dict() for n, r in binding.zones.items()}
    return payload


# ---------------------------------------------------------------------------
# Scaffolds
# ---------------------------------------------------------------------------


@router.get("/scaffolds", response_model=ScaffoldListResponse)
async def list_scaffolds_route(
    format: str | None = Query(default=None),
    vertical: str | None = Query(default=None),
    spec_category: str | None = Query(default=None),
    _user: UserContext = Depends(verify_supabase_jwt),
) -> ScaffoldListResponse:
    rows = await repo.list_scaffolds(
        format=format,
        vertical=vertical,
        spec_category=spec_category,
    )
    items = [_scaffold_to_response(s) for s in rows]
    return ScaffoldListResponse(count=len(items), scaffolds=items)


@router.get("/scaffolds/{slug}", response_model=ScaffoldResponse)
async def get_scaffold_route(
    slug: str,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> ScaffoldResponse:
    s = await repo.get_scaffold_by_slug(slug)
    if s is None:
        raise HTTPException(404, {"error": "scaffold_not_found", "slug": slug})
    return _scaffold_to_response(s)


@router.post(
    "/scaffolds/validate-constraints",
    response_model=SolveResultResponse,
)
async def validate_constraints_route(
    body: ValidateConstraintsRequest,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> SolveResultResponse:
    """Try a constraint spec without saving it. Authoring agent uses this
    in a tight loop to converge on a valid scaffold."""
    dsl, binding, result, err = await run_solve(
        constraint_specification=body.constraint_specification,
        spec_category=body.spec_category,
        spec_variant=body.spec_variant,
        content_config=body.sample_content,
    )
    if err:
        raise HTTPException(400, {"error": "constraint_spec_error", "message": err})
    return _solve_result_to_response(result, binding)


@router.post("/scaffolds", response_model=ScaffoldResponse, status_code=201)
async def create_scaffold_route(
    body: ScaffoldCreateRequest,
    user: UserContext = Depends(require_operator),
) -> ScaffoldResponse:
    # 1. Parse + validate DSL.
    try:
        dsl = ConstraintSpecification.model_validate(body.constraint_specification)
    except Exception as e:
        raise HTTPException(400, {"error": "invalid_constraint_specification", "message": str(e)}) from e
    ref_errors = dsl.validate_references()
    if ref_errors:
        raise HTTPException(400, {"error": "constraint_references", "messages": ref_errors})

    # 2. For each compatible_spec, run the solver against placeholder content
    # — fail creation if the constraints can't be satisfied. This is the
    # acceptance gate the directive calls for.
    for cs in body.compatible_specs:
        binding = await resolve_spec_binding(cs.category, cs.variant)
        if binding is None:
            raise HTTPException(
                400,
                {
                    "error": "unknown_spec",
                    "spec_category": cs.category,
                    "spec_variant": cs.variant,
                },
            )
        intrinsics = derive_intrinsics_from_content(dsl, body.placeholder_content)
        result = solve(
            dsl,
            zones=binding.zones,
            intrinsics=intrinsics,
            content=body.placeholder_content,
        )
        if not result.is_valid:
            raise HTTPException(
                400,
                {
                    "error": "scaffold_does_not_solve",
                    "spec_category": cs.category,
                    "spec_variant": cs.variant,
                    "conflicts": [
                        {
                            "constraint_index": c.constraint_index,
                            "constraint_type": c.constraint_type,
                            "phase": c.phase,
                            "message": c.message,
                        }
                        for c in result.conflicts
                    ],
                },
            )

    # 3. Persist.
    saved = await repo.insert_scaffold(
        slug=body.slug,
        name=body.name,
        description=body.description,
        format=body.format,
        compatible_specs=[cs.model_dump() for cs in body.compatible_specs],
        prop_schema=body.prop_schema,
        constraint_specification=body.constraint_specification,
        preview_image_url=body.preview_image_url,
        vertical_tags=body.vertical_tags,
        is_active=body.is_active,
        version_number=1,
        created_by_user_id=user.business_user_id,
    )
    incr_metric("dmaas.scaffold.created", format=body.format)
    return _scaffold_to_response(saved)


@router.patch("/scaffolds/{slug}", response_model=ScaffoldResponse)
async def update_scaffold_route(
    slug: str,
    body: ScaffoldUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> ScaffoldResponse:
    existing = await repo.get_scaffold_by_slug(slug)
    if existing is None:
        raise HTTPException(404, {"error": "scaffold_not_found", "slug": slug})

    # Validate DSL if provided. Re-run solver against compatible_specs (or
    # the existing ones if unchanged).
    if body.constraint_specification is not None:
        try:
            dsl = ConstraintSpecification.model_validate(body.constraint_specification)
        except Exception as e:
            raise HTTPException(400, {"error": "invalid_constraint_specification", "message": str(e)}) from e
        ref_errors = dsl.validate_references()
        if ref_errors:
            raise HTTPException(400, {"error": "constraint_references", "messages": ref_errors})
        compatible = body.compatible_specs or [
            type("X", (), {"category": cs["category"], "variant": cs["variant"]})()
            for cs in existing.compatible_specs
        ]
        placeholder = body.placeholder_content or {}
        for cs in compatible:
            binding = await resolve_spec_binding(cs.category, cs.variant)
            if binding is None:
                raise HTTPException(
                    400,
                    {"error": "unknown_spec", "spec_category": cs.category, "spec_variant": cs.variant},
                )
            intrinsics = derive_intrinsics_from_content(dsl, placeholder)
            result = solve(dsl, zones=binding.zones, intrinsics=intrinsics, content=placeholder)
            if not result.is_valid:
                raise HTTPException(
                    400,
                    {
                        "error": "scaffold_does_not_solve",
                        "spec_category": cs.category,
                        "spec_variant": cs.variant,
                        "conflicts": [
                            {
                                "constraint_index": c.constraint_index,
                                "constraint_type": c.constraint_type,
                                "phase": c.phase,
                                "message": c.message,
                            }
                            for c in result.conflicts
                        ],
                    },
                )

    fields: dict[str, Any] = {}
    for k in (
        "name",
        "description",
        "preview_image_url",
        "is_active",
    ):
        v = getattr(body, k)
        if v is not None:
            fields[k] = v
    if body.vertical_tags is not None:
        fields["vertical_tags"] = body.vertical_tags
    if body.compatible_specs is not None:
        fields["compatible_specs"] = [cs.model_dump() for cs in body.compatible_specs]
    if body.prop_schema is not None:
        fields["prop_schema"] = body.prop_schema
    if body.constraint_specification is not None:
        fields["constraint_specification"] = body.constraint_specification

    updated = await repo.update_scaffold(slug=slug, fields=fields)
    if updated is None:
        raise HTTPException(404, {"error": "scaffold_not_found", "slug": slug})
    return _scaffold_to_response(updated)


@router.post(
    "/scaffolds/{slug}/preview",
    response_model=SolveResultResponse,
)
async def preview_scaffold_route(
    slug: str,
    body: PreviewRequest,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> SolveResultResponse:
    s = await repo.get_scaffold_by_slug(slug)
    if s is None:
        raise HTTPException(404, {"error": "scaffold_not_found", "slug": slug})
    placeholder = body.placeholder_content or {}
    dsl, binding, result, err = await run_solve(
        constraint_specification=s.constraint_specification,
        spec_category=body.spec_category,
        spec_variant=body.spec_variant,
        content_config=placeholder,
    )
    if err:
        raise HTTPException(400, {"error": "preview_error", "message": err})
    return _solve_result_to_response(result, binding)


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@router.post("/designs", response_model=DesignResponse, status_code=201)
async def create_design_route(
    body: DesignCreateRequest,
    user: UserContext = Depends(verify_supabase_jwt),
) -> DesignResponse:
    scaffold = await repo.get_scaffold_by_id(body.scaffold_id)
    if scaffold is None:
        raise HTTPException(404, {"error": "scaffold_not_found", "scaffold_id": str(body.scaffold_id)})

    schema_errs = validate_content_against_prop_schema(scaffold.prop_schema, body.content_config)
    if schema_errs:
        raise HTTPException(
            400, {"error": "content_schema_violation", "errors": schema_errs}
        )

    dsl, binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=body.spec_category,
        spec_variant=body.spec_variant,
        content_config=body.content_config,
    )
    if err:
        raise HTTPException(400, {"error": "solve_error", "message": err})
    if not result.is_valid:
        raise HTTPException(
            400,
            {
                "error": "design_does_not_solve",
                "conflicts": [
                    {
                        "constraint_index": c.constraint_index,
                        "constraint_type": c.constraint_type,
                        "phase": c.phase,
                        "message": c.message,
                    }
                    for c in result.conflicts
                ],
            },
        )

    saved = await repo.insert_design(
        scaffold_id=body.scaffold_id,
        spec_category=body.spec_category,
        spec_variant=body.spec_variant,
        content_config=body.content_config,
        resolved_positions=result.positions_dict(),
        brand_id=body.brand_id,
        audience_template_id=body.audience_template_id,
        created_by_user_id=user.business_user_id,
    )
    incr_metric("dmaas.design.created", format=scaffold.format)
    return _design_to_response(saved)


@router.get("/designs/{design_id}", response_model=DesignResponse)
async def get_design_route(
    design_id: UUID,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DesignResponse:
    d = await repo.get_design(design_id)
    if d is None:
        raise HTTPException(404, {"error": "design_not_found", "id": str(design_id)})
    return _design_to_response(d)


@router.get("/designs", response_model=DesignListResponse)
async def list_designs_route(
    brand_id: UUID | None = Query(default=None),
    audience_template_id: UUID | None = Query(default=None),
    scaffold_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DesignListResponse:
    rows = await repo.list_designs(
        brand_id=brand_id,
        audience_template_id=audience_template_id,
        scaffold_id=scaffold_id,
        limit=limit,
    )
    items = [_design_to_response(d) for d in rows]
    return DesignListResponse(count=len(items), designs=items)


@router.patch("/designs/{design_id}", response_model=DesignResponse)
async def update_design_route(
    design_id: UUID,
    body: DesignUpdateRequest,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> DesignResponse:
    existing = await repo.get_design(design_id)
    if existing is None:
        raise HTTPException(404, {"error": "design_not_found", "id": str(design_id)})
    scaffold = await repo.get_scaffold_by_id(existing.scaffold_id)
    if scaffold is None:
        raise HTTPException(500, {"error": "scaffold_missing_for_design"})

    schema_errs = validate_content_against_prop_schema(scaffold.prop_schema, body.content_config)
    if schema_errs:
        raise HTTPException(400, {"error": "content_schema_violation", "errors": schema_errs})

    _, _binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=existing.spec_category,
        spec_variant=existing.spec_variant,
        content_config=body.content_config,
    )
    if err:
        raise HTTPException(400, {"error": "solve_error", "message": err})
    if not result.is_valid:
        raise HTTPException(
            400,
            {
                "error": "design_does_not_solve",
                "conflicts": [
                    {
                        "constraint_index": c.constraint_index,
                        "constraint_type": c.constraint_type,
                        "phase": c.phase,
                        "message": c.message,
                    }
                    for c in result.conflicts
                ],
            },
        )
    updated = await repo.update_design_content(
        design_id=design_id,
        content_config=body.content_config,
        resolved_positions=result.positions_dict(),
    )
    if updated is None:
        raise HTTPException(404, {"error": "design_not_found"})
    return _design_to_response(updated)


@router.post("/designs/{design_id}/validate", response_model=SolveResultResponse)
async def validate_design_route(
    design_id: UUID,
    _user: UserContext = Depends(verify_supabase_jwt),
) -> SolveResultResponse:
    d = await repo.get_design(design_id)
    if d is None:
        raise HTTPException(404, {"error": "design_not_found", "id": str(design_id)})
    scaffold = await repo.get_scaffold_by_id(d.scaffold_id)
    if scaffold is None:
        raise HTTPException(500, {"error": "scaffold_missing_for_design"})
    _, binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=d.spec_category,
        spec_variant=d.spec_variant,
        content_config=d.content_config,
    )
    if err:
        raise HTTPException(400, {"error": "solve_error", "message": err})
    return _solve_result_to_response(result, binding)


# ---------------------------------------------------------------------------
# Authoring sessions
# ---------------------------------------------------------------------------


def _authoring_to_response(a) -> AuthoringSessionResponse:
    return AuthoringSessionResponse(
        id=a.id,
        scaffold_id=a.scaffold_id,
        prompt=a.prompt,
        proposed_constraint_specification=a.proposed_constraint_specification,
        accepted=a.accepted,
        notes=a.notes,
        created_at=a.created_at.isoformat(),
    )


@router.post(
    "/scaffold-authoring-sessions",
    response_model=AuthoringSessionResponse,
    status_code=201,
)
async def create_authoring_session_route(
    body: AuthoringSessionCreateRequest,
    user: UserContext = Depends(require_operator),
) -> AuthoringSessionResponse:
    saved = await repo.insert_authoring_session(
        scaffold_id=body.scaffold_id,
        prompt=body.prompt,
        proposed_constraint_specification=body.proposed_constraint_specification,
        accepted=body.accepted,
        notes=body.notes,
        created_by_user_id=user.business_user_id,
    )
    return _authoring_to_response(saved)


@router.get(
    "/scaffold-authoring-sessions",
    response_model=AuthoringSessionListResponse,
)
async def list_authoring_sessions_route(
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserContext = Depends(require_operator),
) -> AuthoringSessionListResponse:
    rows = await repo.list_authoring_sessions(limit=limit)
    return AuthoringSessionListResponse(
        count=len(rows),
        sessions=[_authoring_to_response(a) for a in rows],
    )
