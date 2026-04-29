"""MCP server exposing DMaaS endpoints as MCP tools.

Mounted under `/mcp` on the FastAPI app via FastMCP's `http_app()`. Managed
agents connect via standard MCP HTTP transport; auth is the same Supabase
JWT used by the REST router (header passes through to the wrapped
dependencies).

Each tool is a thin wrapper around the corresponding service-layer call —
identical to the REST router's behavior, but surfaced with a description
the LLM uses to pick the right tool for the job.

The tools intentionally do NOT call FastAPI's HTTP layer. They reach
directly into the service / repository modules so the call path is short
and there's no network hop or duplicated marshalling.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastmcp import FastMCP

from app.direct_mail import specs as direct_mail_specs
from app.dmaas import repository as repo
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.service import (
    binding_to_dict,
    derive_intrinsics_from_content,
    resolve_spec_binding,
    run_solve,
    validate_content_against_prop_schema,
)
from app.dmaas.solver import solve

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="hq-x DMaaS",
    instructions=(
        "Tools for managing direct-mail scaffolds (layout templates), "
        "designs (per-mailer content + resolved positions), and validating "
        "constraint specifications against Lob mailer specs. "
        "Workflow for the scaffold-authoring agent: call validate_constraints "
        "in a tight loop until the proposal solves, then create_scaffold. "
        "Workflow for the content agent: call list_scaffolds + get_scaffold "
        "to pick a layout, then create_design with content_config; the "
        "server runs the solver and persists resolved_positions."
    ),
)


# ---------------------------------------------------------------------------
# Output marshallers (return plain dicts so MCP serializes cleanly)
# ---------------------------------------------------------------------------


def _scaffold_dict(s) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "slug": s.slug,
        "name": s.name,
        "description": s.description,
        "format": s.format,
        "compatible_specs": s.compatible_specs,
        "prop_schema": s.prop_schema,
        "constraint_specification": s.constraint_specification,
        "preview_image_url": s.preview_image_url,
        "vertical_tags": s.vertical_tags,
        "is_active": s.is_active,
        "version_number": s.version_number,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _design_dict(d) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "scaffold_id": str(d.scaffold_id),
        "spec_category": d.spec_category,
        "spec_variant": d.spec_variant,
        "content_config": d.content_config,
        "resolved_positions": d.resolved_positions,
        "brand_id": str(d.brand_id) if d.brand_id else None,
        "audience_template_id": str(d.audience_template_id) if d.audience_template_id else None,
        "version_number": d.version_number,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


def _solve_envelope(result, binding=None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "is_valid": result.is_valid,
        "positions": result.positions_dict(),
        "conflicts": [
            {
                "constraint_index": c.constraint_index,
                "constraint_type": c.constraint_type,
                "phase": c.phase,
                "message": c.message,
                "detail": c.detail,
            }
            for c in result.conflicts
        ],
    }
    if binding is not None:
        out["canvas"] = binding.canvas.to_dict()
        out["zones"] = {n: r.to_dict() for n, r in binding.zones.items()}
    return out


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_specs(category: str | None = None) -> dict[str, Any]:
    """List Lob mailer specs the DMaaS agent can author scaffolds against.

    Returns a catalog of (category, variant, label) plus minimal geometry
    (bleed, trim, full_bleed flag, addressable face count) so the agent
    can pick a target before calling `get_spec` for full zone detail.
    Optional `category` filter narrows to one mailer category
    (postcard, self_mailer, letter, …)."""
    rows = await direct_mail_specs.list_specs(category=category)
    return {
        "count": len(rows),
        "specs": [
            {
                "id": s.id,
                "mailer_category": s.mailer_category,
                "variant": s.variant,
                "label": s.label,
                "bleed_w_in": s.bleed_w_in,
                "bleed_h_in": s.bleed_h_in,
                "trim_w_in": s.trim_w_in,
                "trim_h_in": s.trim_h_in,
                "full_bleed": bool((s.production or {}).get("full_bleed")),
                "addressable_face_count": sum(
                    1 for f in (s.faces or []) if f.get("is_addressable")
                ),
                "has_faces_v2": bool(s.faces),
            }
            for s in rows
        ],
    }


@mcp.tool
async def get_spec(category: str, variant: str) -> dict[str, Any]:
    """Fetch one mailer spec with its full resolved zone catalog.

    Returns the spec record AND the binding the solver consumes —
    every named zone with absolute pixel coordinates plus typed region
    metadata (face, panel, source, aliases). Call this once per
    scaffold-authoring session to learn what zone names you can
    reference in the constraint DSL.

    Zone naming for v1:
      * Postcards: `front_face`, `back_face`, `front_safe`, `back_safe`,
        `back_address_block`, `back_postage_indicia`,
        `back_return_address`, `back_usps_barcode_clear`,
        `back_ink_free` (alias of address_block),
        `front_usps_scan_warning`.
      * Self-mailer bifolds: `outside_face`, `inside_face`, per-panel
        rectangles (`outside_top_panel`, `outside_top_panel_safe`, …),
        `outside_address_window`, `outside_postage_indicia`,
        `outside_usps_barcode_clear`, `glue_zone_top`/`_bottom`/
        `_left`/`_right` (per opening edges), `fold_gutter_1`.
      * Plus legacy: `safe_zone`, `ink_free`, `usps_scan_warning`,
        `canvas`, `trim`.
    """
    binding = await resolve_spec_binding(category, variant)
    if binding is None:
        return {"error": "spec_not_found", "category": category, "variant": variant}
    return binding_to_dict(binding)


@mcp.tool
async def list_scaffolds(
    format: str | None = None,
    vertical: str | None = None,
    spec_category: str | None = None,
) -> dict[str, Any]:
    """List active DMaaS scaffolds, optionally filtered by `format`
    (postcard, letter, ...), `vertical` tag (trucking, factoring, ...),
    or `spec_category` (must be one of the spec categories the scaffold
    is marked compatible with). Returns scaffold summaries the content
    agent can pick from."""
    rows = await repo.list_scaffolds(
        format=format, vertical=vertical, spec_category=spec_category
    )
    return {"count": len(rows), "scaffolds": [_scaffold_dict(s) for s in rows]}


@mcp.tool
async def get_scaffold(slug: str) -> dict[str, Any]:
    """Fetch one scaffold by slug. Returns the full scaffold record
    including `prop_schema` (what content_config must look like) and
    `constraint_specification` (the layout DSL)."""
    s = await repo.get_scaffold_by_slug(slug)
    if s is None:
        return {"error": "scaffold_not_found", "slug": slug}
    return _scaffold_dict(s)


# ---------------------------------------------------------------------------
# Constraint testing (used by scaffold-authoring agent)
# ---------------------------------------------------------------------------


@mcp.tool
async def validate_constraints(
    spec_category: str,
    spec_variant: str,
    constraint_specification: dict[str, Any],
    sample_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a proposed constraint_specification against a (spec_category,
    spec_variant) without saving. Returns is_valid + positions when the
    spec solves, or structured conflicts when it doesn't. Use this in a
    refine-then-test loop before calling create_scaffold."""
    dsl, binding, result, err = await run_solve(
        constraint_specification=constraint_specification,
        spec_category=spec_category,
        spec_variant=spec_variant,
        content_config=sample_content or {},
    )
    if err:
        return {"error": "constraint_spec_error", "message": err}
    return _solve_envelope(result, binding)


@mcp.tool
async def preview_scaffold(
    slug: str,
    spec_category: str,
    spec_variant: str,
    placeholder_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a saved scaffold against the given spec with placeholder
    content; returns resolved positions + zone geometry the frontend
    canvas needs."""
    s = await repo.get_scaffold_by_slug(slug)
    if s is None:
        return {"error": "scaffold_not_found", "slug": slug}
    dsl, binding, result, err = await run_solve(
        constraint_specification=s.constraint_specification,
        spec_category=spec_category,
        spec_variant=spec_variant,
        content_config=placeholder_content or {},
    )
    if err:
        return {"error": "preview_error", "message": err}
    return _solve_envelope(result, binding)


# ---------------------------------------------------------------------------
# Write tools (operator-only at the REST layer; MCP enforcement TBD via
# transport-layer auth — see app/main.py for mount-time auth wiring)
# ---------------------------------------------------------------------------


@mcp.tool
async def create_scaffold(
    slug: str,
    name: str,
    format: str,
    constraint_specification: dict[str, Any],
    description: str | None = None,
    compatible_specs: list[dict[str, str]] | None = None,
    prop_schema: dict[str, Any] | None = None,
    preview_image_url: str | None = None,
    vertical_tags: list[str] | None = None,
    placeholder_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a new scaffold. Validates the constraint_specification +
    runs the solver against every entry in compatible_specs using
    placeholder_content; refuses to save if any combination doesn't solve."""
    try:
        dsl = ConstraintSpecification.model_validate(constraint_specification)
    except Exception as e:
        return {"error": "invalid_constraint_specification", "message": str(e)}
    ref_errors = dsl.validate_references()
    if ref_errors:
        return {"error": "constraint_references", "messages": ref_errors}

    compatible_specs = compatible_specs or []
    placeholder = placeholder_content or {}
    for cs in compatible_specs:
        binding = await resolve_spec_binding(cs["category"], cs["variant"])
        if binding is None:
            return {"error": "unknown_spec", "spec": cs}
        result = solve(
            dsl,
            zones=binding.zones,
            intrinsics=derive_intrinsics_from_content(dsl, placeholder),
            content=placeholder,
        )
        if not result.is_valid:
            return {
                "error": "scaffold_does_not_solve",
                "spec": cs,
                "conflicts": [
                    {
                        "constraint_index": c.constraint_index,
                        "constraint_type": c.constraint_type,
                        "phase": c.phase,
                        "message": c.message,
                    }
                    for c in result.conflicts
                ],
            }

    saved = await repo.insert_scaffold(
        slug=slug,
        name=name,
        description=description,
        format=format,
        compatible_specs=compatible_specs,
        prop_schema=prop_schema or {},
        constraint_specification=constraint_specification,
        preview_image_url=preview_image_url,
        vertical_tags=vertical_tags or [],
        is_active=True,
        version_number=1,
        created_by_user_id=None,
    )
    return _scaffold_dict(saved)


@mcp.tool
async def update_scaffold(
    slug: str,
    name: str | None = None,
    description: str | None = None,
    constraint_specification: dict[str, Any] | None = None,
    compatible_specs: list[dict[str, str]] | None = None,
    prop_schema: dict[str, Any] | None = None,
    preview_image_url: str | None = None,
    vertical_tags: list[str] | None = None,
    is_active: bool | None = None,
    placeholder_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch an existing scaffold. If constraint_specification is changed,
    the solver re-runs against compatible_specs (or the existing list)."""
    existing = await repo.get_scaffold_by_slug(slug)
    if existing is None:
        return {"error": "scaffold_not_found", "slug": slug}

    if constraint_specification is not None:
        try:
            dsl = ConstraintSpecification.model_validate(constraint_specification)
        except Exception as e:
            return {"error": "invalid_constraint_specification", "message": str(e)}
        ref_errors = dsl.validate_references()
        if ref_errors:
            return {"error": "constraint_references", "messages": ref_errors}
        check_specs = compatible_specs or existing.compatible_specs
        placeholder = placeholder_content or {}
        for cs in check_specs:
            binding = await resolve_spec_binding(cs["category"], cs["variant"])
            if binding is None:
                return {"error": "unknown_spec", "spec": cs}
            result = solve(
                dsl,
                zones=binding.zones,
                intrinsics=derive_intrinsics_from_content(dsl, placeholder),
                content=placeholder,
            )
            if not result.is_valid:
                return {
                    "error": "scaffold_does_not_solve",
                    "spec": cs,
                    "conflicts": [
                        {
                            "constraint_index": c.constraint_index,
                            "constraint_type": c.constraint_type,
                            "phase": c.phase,
                            "message": c.message,
                        }
                        for c in result.conflicts
                    ],
                }

    fields: dict[str, Any] = {}
    for k, v in {
        "name": name,
        "description": description,
        "preview_image_url": preview_image_url,
        "is_active": is_active,
        "constraint_specification": constraint_specification,
        "prop_schema": prop_schema,
        "vertical_tags": vertical_tags,
        "compatible_specs": compatible_specs,
    }.items():
        if v is not None:
            fields[k] = v

    updated = await repo.update_scaffold(slug=slug, fields=fields)
    return _scaffold_dict(updated) if updated else {"error": "scaffold_not_found"}


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@mcp.tool
async def create_design(
    scaffold_id: str,
    spec_category: str,
    spec_variant: str,
    content_config: dict[str, Any],
    brand_id: str | None = None,
    audience_template_id: str | None = None,
) -> dict[str, Any]:
    """Create a new mailer design. Validates content_config against the
    scaffold's prop_schema, runs the solver, persists with cached
    resolved_positions. Returns the saved Design or a structured error
    when the design doesn't solve."""
    scaffold = await repo.get_scaffold_by_id(UUID(scaffold_id))
    if scaffold is None:
        return {"error": "scaffold_not_found", "scaffold_id": scaffold_id}

    schema_errs = validate_content_against_prop_schema(scaffold.prop_schema, content_config)
    if schema_errs:
        return {"error": "content_schema_violation", "errors": schema_errs}

    _, binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=spec_category,
        spec_variant=spec_variant,
        content_config=content_config,
    )
    if err:
        return {"error": "solve_error", "message": err}
    if not result.is_valid:
        return {
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
        }

    saved = await repo.insert_design(
        scaffold_id=UUID(scaffold_id),
        spec_category=spec_category,
        spec_variant=spec_variant,
        content_config=content_config,
        resolved_positions=result.positions_dict(),
        brand_id=UUID(brand_id) if brand_id else None,
        audience_template_id=UUID(audience_template_id) if audience_template_id else None,
        created_by_user_id=None,
    )
    return _design_dict(saved)


@mcp.tool
async def get_design(id: str) -> dict[str, Any]:
    """Fetch a design by UUID. Returns content_config + cached resolved_positions."""
    d = await repo.get_design(UUID(id))
    return _design_dict(d) if d else {"error": "design_not_found", "id": id}


@mcp.tool
async def update_design_content(id: str, content_config: dict[str, Any]) -> dict[str, Any]:
    """Replace a design's content_config. Re-validates against prop_schema,
    re-solves, updates cached resolved_positions."""
    existing = await repo.get_design(UUID(id))
    if existing is None:
        return {"error": "design_not_found", "id": id}
    scaffold = await repo.get_scaffold_by_id(existing.scaffold_id)
    if scaffold is None:
        return {"error": "scaffold_missing_for_design"}

    schema_errs = validate_content_against_prop_schema(scaffold.prop_schema, content_config)
    if schema_errs:
        return {"error": "content_schema_violation", "errors": schema_errs}

    _, _binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=existing.spec_category,
        spec_variant=existing.spec_variant,
        content_config=content_config,
    )
    if err:
        return {"error": "solve_error", "message": err}
    if not result.is_valid:
        return {
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
        }

    updated = await repo.update_design_content(
        design_id=UUID(id),
        content_config=content_config,
        resolved_positions=result.positions_dict(),
    )
    return _design_dict(updated) if updated else {"error": "design_not_found"}


@mcp.tool
async def validate_design(id: str) -> dict[str, Any]:
    """Re-run the solver on a saved design's current content. Useful when
    a scaffold's constraint_specification has been updated and you want
    to confirm the design still resolves."""
    d = await repo.get_design(UUID(id))
    if d is None:
        return {"error": "design_not_found", "id": id}
    scaffold = await repo.get_scaffold_by_id(d.scaffold_id)
    if scaffold is None:
        return {"error": "scaffold_missing_for_design"}
    _, binding, result, err = await run_solve(
        constraint_specification=scaffold.constraint_specification,
        spec_category=d.spec_category,
        spec_variant=d.spec_variant,
        content_config=d.content_config,
    )
    if err:
        return {"error": "solve_error", "message": err}
    return _solve_envelope(result, binding)
