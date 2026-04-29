"""Service layer that wires direct_mail_specs (zone geometry) and the
constraint solver. The router calls this; this calls the solver +
repository + spec lookups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jsonschema

from app.direct_mail.specs import MailerSpec, get_spec
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.solver import (
    ElementIntrinsics,
    Rect,
    SolveResult,
    solve,
)


@dataclass
class SpecBinding:
    """Resolved zone geometry (in pixels @ DPI) for a given (category, variant)."""

    spec: MailerSpec
    dpi: int
    canvas: Rect  # the full bleed (or trim, if no bleed) area in pixels
    zones: dict[str, Rect]  # zone name → pixel Rect


def _to_pixel_rect(width_in: float, height_in: float, dpi: int, *, x_in: float = 0.0, y_in: float = 0.0) -> Rect:
    return Rect(x=x_in * dpi, y=y_in * dpi, w=width_in * dpi, h=height_in * dpi)


def bind_spec_zones(spec: MailerSpec) -> SpecBinding:
    """Translate a MailerSpec's inches-based zones into pixel-space Rects.

    The canvas origin is top-left of the bleed area (or trim area if there
    is no bleed). Zones get pinned to absolute pixel coordinates the solver
    can read directly.

    Each direct_mail_specs zone is one of:
      * A pure dimension descriptor with anchor offsets — e.g.
        `{w_in, h_in, from_right_in, from_bottom_in}`. We translate to an
        absolute Rect using the canvas dimensions.
      * A label-only annotation with no rectangle (e.g. usps_scan_warning's
        anchor=bottom but no width). Skipped here.
    The frontend renders zones using the same data; this is the canonical
    server-side translation."""
    dpi = int(spec.production.get("required_dpi", 300))
    cw_in = spec.bleed_w_in if spec.bleed_w_in else spec.trim_w_in
    ch_in = spec.bleed_h_in if spec.bleed_h_in else spec.trim_h_in
    canvas = _to_pixel_rect(cw_in, ch_in, dpi)

    zones: dict[str, Rect] = {"canvas": canvas}

    # Trim zone (= canvas if no bleed; inset by 0.125" if there is bleed).
    if spec.bleed_w_in and spec.bleed_h_in:
        bleed_x_in = (spec.bleed_w_in - spec.trim_w_in) / 2
        bleed_y_in = (spec.bleed_h_in - spec.trim_h_in) / 2
        zones["trim"] = _to_pixel_rect(
            spec.trim_w_in, spec.trim_h_in, dpi, x_in=bleed_x_in, y_in=bleed_y_in
        )
    else:
        zones["trim"] = Rect(x=0, y=0, w=canvas.w, h=canvas.h)

    # Safe zone (trim minus safe_inset all sides).
    if spec.safe_inset_in is not None:
        safe_inset = spec.safe_inset_in * dpi
        t = zones["trim"]
        zones["safe_zone"] = Rect(
            x=t.x + safe_inset,
            y=t.y + safe_inset,
            w=max(0, t.w - 2 * safe_inset),
            h=max(0, t.h - 2 * safe_inset),
        )

    # Named zones from `zones` JSON (ink_free, address_block, envelope_window,
    # binding_zone, etc.). Each one's coordinate model varies; we handle the
    # common forms. Future zone shapes can be added here.
    for name, z in spec.zones.items():
        if not isinstance(z, dict):
            continue
        rect = _zone_dict_to_rect(z, canvas, dpi)
        if rect is not None:
            zones[name] = rect

    return SpecBinding(spec=spec, dpi=dpi, canvas=canvas, zones=zones)


def _zone_dict_to_rect(z: dict, canvas: Rect, dpi: int) -> Rect | None:
    """Best-effort translation of one zones[name] dict into a pixel Rect.

    Handles three coordinate styles:
      * Anchored from edges: `from_right_in` / `from_bottom_in` / `from_left_in` / `from_top_in`
      * Anchored at a corner: `from_left_in` + `from_top_in`
      * Pure size with no anchor: w_in/h_in only — interpreted as floating;
        we emit a zone at canvas top-left, which is informational only.
    Zones without w_in/h_in are skipped (they're labels, not rectangles).
    """
    w_in = z.get("w_in")
    h_in = z.get("h_in")
    if w_in is None or h_in is None:
        return None

    w = w_in * dpi
    h = h_in * dpi

    # Y position
    if "from_top_in" in z:
        y = z["from_top_in"] * dpi
    elif "from_bottom_in" in z:
        y = canvas.h - h - z["from_bottom_in"] * dpi
    else:
        y = 0.0

    # X position
    if "from_left_in" in z:
        x = z["from_left_in"] * dpi
    elif "from_right_in" in z:
        x = canvas.w - w - z["from_right_in"] * dpi
    else:
        x = 0.0

    return Rect(x=x, y=y, w=w, h=h)


# ---------------------------------------------------------------------------
# Content + intrinsics extraction
# ---------------------------------------------------------------------------


def derive_intrinsics_from_content(
    spec: ConstraintSpecification,
    content_config: dict[str, Any],
) -> dict[str, ElementIntrinsics]:
    """Best-effort element-size hints from content_config.

    Convention: `content_config[element_name]` may contain:
      * `intrinsic`: {min_width, min_height, max_width, max_height,
        preferred_width, preferred_height}  — caller-computed text metrics
      * (anything else is content; ignored for sizing)

    Elements without `intrinsic` get default ElementIntrinsics() — solver
    falls back to constraints alone."""
    out: dict[str, ElementIntrinsics] = {}
    for name in spec.elements:
        intr = (content_config.get(name) or {}).get("intrinsic", {})
        out[name] = ElementIntrinsics(
            min_width=intr.get("min_width", 0.0),
            min_height=intr.get("min_height", 0.0),
            max_width=intr.get("max_width"),
            max_height=intr.get("max_height"),
            preferred_width=intr.get("preferred_width"),
            preferred_height=intr.get("preferred_height"),
        )
    return out


# ---------------------------------------------------------------------------
# Top-level solve helper
# ---------------------------------------------------------------------------


async def resolve_spec_binding(category: str, variant: str) -> SpecBinding | None:
    spec = await get_spec(category, variant)
    if spec is None:
        return None
    return bind_spec_zones(spec)


async def run_solve(
    *,
    constraint_specification: dict[str, Any],
    spec_category: str,
    spec_variant: str,
    content_config: dict[str, Any],
) -> tuple[ConstraintSpecification | None, SpecBinding | None, SolveResult | None, str | None]:
    """Glue: parse the DSL, bind the spec, run the solver. Returns
    (spec_dsl, binding, result, error_message). Any of the first three may
    be None if a prior step failed."""
    try:
        dsl = ConstraintSpecification.model_validate(constraint_specification)
    except Exception as e:
        return None, None, None, f"invalid constraint_specification: {e}"
    binding = await resolve_spec_binding(spec_category, spec_variant)
    if binding is None:
        return dsl, None, None, f"unknown spec ({spec_category}, {spec_variant})"
    intrinsics = derive_intrinsics_from_content(dsl, content_config)
    result = solve(
        dsl,
        zones=binding.zones,
        intrinsics=intrinsics,
        content=content_config,
    )
    return dsl, binding, result, None


def validate_content_against_prop_schema(
    prop_schema: dict[str, Any],
    content_config: dict[str, Any],
) -> list[str]:
    """JSON Schema validate. Empty list = OK. Each error is one path:msg."""
    if not prop_schema:
        return []
    validator = jsonschema.Draft202012Validator(prop_schema)
    errors = []
    for err in validator.iter_errors(content_config):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return errors
