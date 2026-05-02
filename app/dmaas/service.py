"""Service layer that wires direct_mail_specs (zone geometry) and the
constraint solver. The router calls this; this calls the solver +
repository + spec lookups.

Zone-resolution model (v2)
--------------------------

A `MailerSpec` row is the substrate. `bind_spec_zones` turns it into a
`SpecBinding` that the solver can consume. The binding exposes:

  * `canvas` — the bleed (or trim, when no bleed) rectangle in pixels.
    Single-canvas-per-face: front/back of a postcard share the same
    canvas (overlaid), as do outside/inside of a self-mailer. A scaffold
    designs for ONE face at a time; zones are face-namespaced so the
    author picks which face's geometry applies.
  * `zones` — flat `{name: Rect}` map the DSL solver consumes verbatim.
    Includes legacy names (`canvas`, `trim`, `safe_zone`, plus any
    `direct_mail_specs.zones` rectangles) AND v2 face-namespaced names
    (`front_safe`, `back_address_block`, `outside_top_panel`, …).
  * `regions` — typed list of zone descriptors with `face`, `type`, and
    `source` metadata. The MCP `get_spec` tool returns this so the
    scaffold-authoring agent knows which zone is what.
  * `faces` — list of face descriptors (name, is_addressable, rect).

What's derived vs stored
------------------------

  * Postcard zones (face surfaces, safe insets, address_block, postage,
    return_address, barcode_clear, scan_warning) are computed from
    `MailerSpec.faces` plus bleed/trim dims. The faces JSONB stores
    rect-from-edge anchors; we resolve them here.
  * Self-mailer panel rectangles are computed from `folding` metadata
    (fold lines, panel offset). Cover-panel zones (address_window,
    postage, barcode_clear) come from `MailerSpec.faces[].panel_zones`
    in panel-local coords; this code positions them within the
    derived panel rect.
  * Glue zones and fold gutters are computed from `folding.opening_edges`
    + `glue_zone_width_in` + `fold_gutter_half_width_in`.

Choosing the location axis (data vs derive) per zone:

  * Panel rectangles → DERIVED. `folding` already has the geometry.
    Denormalizing means drift risk on a fold-line edit.
  * Address window position on cover panel → STORED in `faces`. Lob
    publishes a panel-local offset that's not derivable from folding.
  * Postage / return_address / barcode_clear → STORED in `faces` per
    face/panel. USPS DMM convention; not derivable.
  * Glue zones / fold gutters → DERIVED. Width and edge list are
    constants we put in `folding`; emitting rectangles each bind keeps
    the data lean.

Adding a future format (snap-pack, letter, letter_envelope, trifold):
the model accommodates them additively. Trifold needs only a
length-2 `fold_lines_in_from_left` and the existing panel-derivation
loop produces 3 panels. Snap-pack needs a new region type
(`perforation`) and an `outside_face`/`inside_face` model with no
fold — i.e. one face descriptor per side. Letter needs an optional
`attachments` list on the face. Letter envelope needs a
`window_cutout` region type. None of these require touching
`bind_spec_zones`'s public signature or the `zones` flat-map shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FaceDescriptor:
    name: str
    rect: Rect  # in canvas-pixel coords (face origin == canvas origin for v1)
    is_addressable: bool
    cover_panel: str | None = None  # self-mailer only


@dataclass
class RegionDescriptor:
    """Typed zone metadata. The solver only sees `zones[name] -> Rect`,
    but the MCP-facing serializer returns these so the agent knows which
    zone is the address block, which is informational, etc."""

    name: str
    type: str  # "face" | "safe" | "panel" | "address_block" | "address_window"
              # | "postage" | "barcode_clear" | "return_address"
              # | "informational" | "fold_gutter" | "glue"
              # | "ink_free"  (legacy alias)
              # | "window_cutout" | "perforation"  (future formats)
    rect: Rect
    face: str | None = None
    panel: str | None = None
    derived_from: str | None = None
    source: str | None = None  # "lob_help_center" | "usps_dmm" | "derived"
    aliases: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class SpecBinding:
    """Resolved zone geometry (in pixels @ DPI) for a given (category, variant)."""

    spec: MailerSpec
    dpi: int
    canvas: Rect  # the full bleed (or trim, if no bleed) area in pixels
    zones: dict[str, Rect]  # zone name → pixel Rect (solver-facing flat map)
    regions: list[RegionDescriptor] = field(default_factory=list)
    faces: list[FaceDescriptor] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_pixel_rect(width_in: float, height_in: float, dpi: int, *, x_in: float = 0.0, y_in: float = 0.0) -> Rect:
    return Rect(x=x_in * dpi, y=y_in * dpi, w=width_in * dpi, h=height_in * dpi)


def _resolve_rect_in(rect_in: dict[str, Any], parent_w_in: float, parent_h_in: float) -> tuple[float, float, float, float]:
    """Resolve a rect-from-edge descriptor against a parent rectangle.

    Supported keys: w, h, w_full_face, h_full_face, from_left, from_right,
    from_top, from_bottom. Returns (x_in, y_in, w_in, h_in) in parent-local
    coordinates. Origin is parent top-left; +y is down.
    """
    if rect_in.get("w_full_face"):
        w_in = parent_w_in
    else:
        w_in = float(rect_in.get("w", 0.0))
    if rect_in.get("h_full_face"):
        h_in = parent_h_in
    else:
        h_in = float(rect_in.get("h", 0.0))

    if "from_left" in rect_in:
        x_in = float(rect_in["from_left"])
    elif "from_right" in rect_in:
        x_in = parent_w_in - w_in - float(rect_in["from_right"])
    else:
        x_in = 0.0

    if "from_top" in rect_in:
        y_in = float(rect_in["from_top"])
    elif "from_bottom" in rect_in:
        y_in = parent_h_in - h_in - float(rect_in["from_bottom"])
    else:
        y_in = 0.0

    return x_in, y_in, w_in, h_in


def _zone_dict_to_rect(z: dict, canvas: Rect, dpi: int) -> Rect | None:
    """Best-effort translation of one legacy zones[name] dict into a pixel Rect.

    Handles three coordinate styles:
      * Anchored from edges: from_right_in / from_bottom_in / from_left_in / from_top_in
      * Anchored at a corner: from_left_in + from_top_in
      * Pure size with no anchor: w_in/h_in only — interpreted as floating; we
        emit a zone at canvas top-left, which is informational only.
    Zones without w_in/h_in are skipped (they're labels, not rectangles).
    """
    w_in = z.get("w_in")
    h_in = z.get("h_in")
    if w_in is None or h_in is None:
        return None

    w = w_in * dpi
    h = h_in * dpi

    if "from_top_in" in z:
        y = z["from_top_in"] * dpi
    elif "from_bottom_in" in z:
        y = canvas.h - h - z["from_bottom_in"] * dpi
    else:
        y = 0.0

    if "from_left_in" in z:
        x = z["from_left_in"] * dpi
    elif "from_right_in" in z:
        x = canvas.w - w - z["from_right_in"] * dpi
    else:
        x = 0.0

    return Rect(x=x, y=y, w=w, h=h)


# ---------------------------------------------------------------------------
# Self-mailer panel derivation
# ---------------------------------------------------------------------------


_VERTICAL_PANEL_NAMES = {
    "left_right": ["outside_left_panel", "outside_right_panel"],
    "top_bottom": ["outside_top_panel", "outside_bottom_panel"],
    "left_middle_right": [
        "outside_left_panel",
        "outside_middle_panel",
        "outside_right_panel",
    ],
}


def _panel_names(folding: dict[str, Any], face_name: str) -> list[str]:
    """Resolve panel names for a face from `folding.panel_naming` + naming
    convention. Returns the per-face panel names (e.g. inside variants
    swap the `outside_` prefix for `inside_`)."""
    naming = folding.get("panel_naming") or "left_right"
    base = _VERTICAL_PANEL_NAMES.get(naming)
    if base is None:
        # Fallback: numbered panels
        n = int(folding.get("panel_count") or 2)
        base = [f"outside_panel_{i + 1}" for i in range(n)]
    return [n.replace("outside_", f"{face_name}_") for n in base]


def _derive_panels(spec: MailerSpec, dpi: int) -> dict[str, dict[str, Rect]]:
    """For each face, return {panel_name: panel_rect_in_canvas_coords}.

    Panel rectangles are computed from fold-line metadata. Trim insets
    (the bleed margin) are applied so panels live in canvas (bleed)
    coordinates. The same panel rectangle is reused for outside and
    inside faces — the canvas-per-face model means both faces share the
    canvas; only the panel name prefix changes.
    """
    folding = spec.folding or {}
    bleed_w = (spec.bleed_w_in or spec.trim_w_in)
    bleed_h = (spec.bleed_h_in or spec.trim_h_in)
    trim_w = spec.trim_w_in
    trim_h = spec.trim_h_in
    bleed_margin_x = (bleed_w - trim_w) / 2.0
    bleed_margin_y = (bleed_h - trim_h) / 2.0

    fold_xs = folding.get("fold_lines_in_from_left")
    fold_ys = folding.get("fold_lines_in_from_top")

    out: dict[str, dict[str, Rect]] = {}
    if fold_xs:
        # Vertical fold(s) → panels split L→R across the trim width.
        boundaries_in = [0.0] + sorted(float(x) for x in fold_xs) + [trim_w]
        # Iterate per face (outside/inside) using folding.panel_naming for names
        for face_name in ("outside", "inside"):
            face_panels = {}
            names = _panel_names(folding, face_name)
            # If we somehow have a mismatch between fold count and naming,
            # fall back to numbered panels.
            if len(names) != len(boundaries_in) - 1:
                names = [f"{face_name}_panel_{i + 1}" for i in range(len(boundaries_in) - 1)]
            for i, name in enumerate(names):
                x_in_canvas = bleed_margin_x + boundaries_in[i]
                w_in_canvas = boundaries_in[i + 1] - boundaries_in[i]
                rect = _to_pixel_rect(
                    w_in_canvas,
                    trim_h,
                    dpi,
                    x_in=x_in_canvas,
                    y_in=bleed_margin_y,
                )
                face_panels[name] = rect
            out[face_name] = face_panels
    elif fold_ys:
        # Horizontal fold(s) → panels split T→B across the trim height.
        boundaries_in = [0.0] + sorted(float(y) for y in fold_ys) + [trim_h]
        for face_name in ("outside", "inside"):
            face_panels = {}
            names = _panel_names(folding, face_name)
            if len(names) != len(boundaries_in) - 1:
                names = [f"{face_name}_panel_{i + 1}" for i in range(len(boundaries_in) - 1)]
            for i, name in enumerate(names):
                y_in_canvas = bleed_margin_y + boundaries_in[i]
                h_in_canvas = boundaries_in[i + 1] - boundaries_in[i]
                rect = _to_pixel_rect(
                    trim_w,
                    h_in_canvas,
                    dpi,
                    x_in=bleed_margin_x,
                    y_in=y_in_canvas,
                )
                face_panels[name] = rect
            out[face_name] = face_panels
    return out


def _derive_glue_and_fold_zones(
    spec: MailerSpec,
    dpi: int,
    panels_by_face: dict[str, dict[str, Rect]],
) -> list[RegionDescriptor]:
    """Glue strips along opening edges + fold gutters straddling fold lines."""
    folding = spec.folding or {}
    if not folding:
        return []
    glue_w_in = float(folding.get("glue_zone_width_in") or 0.25)
    gutter_half_in = float(folding.get("fold_gutter_half_width_in") or 0.125)
    bleed_w = (spec.bleed_w_in or spec.trim_w_in)
    bleed_h = (spec.bleed_h_in or spec.trim_h_in)
    trim_w = spec.trim_w_in
    trim_h = spec.trim_h_in
    bleed_margin_x = (bleed_w - trim_w) / 2.0
    bleed_margin_y = (bleed_h - trim_h) / 2.0

    out: list[RegionDescriptor] = []

    # --- Glue zones, outside face only (the side that gets sealed) ---
    opening_edges = folding.get("opening_edges") or []
    for edge in opening_edges:
        if edge == "top":
            rect = _to_pixel_rect(
                trim_w, glue_w_in, dpi,
                x_in=bleed_margin_x, y_in=bleed_margin_y,
            )
            out.append(RegionDescriptor(
                name="glue_zone_top", type="glue", rect=rect, face="outside",
                source="derived",
                note=f"{glue_w_in}\" along top opening edge",
            ))
        elif edge == "bottom":
            rect = _to_pixel_rect(
                trim_w, glue_w_in, dpi,
                x_in=bleed_margin_x,
                y_in=bleed_margin_y + trim_h - glue_w_in,
            )
            out.append(RegionDescriptor(
                name="glue_zone_bottom", type="glue", rect=rect, face="outside",
                source="derived",
                note=f"{glue_w_in}\" along bottom opening edge",
            ))
        elif edge == "left_outer":
            rect = _to_pixel_rect(
                glue_w_in, trim_h, dpi,
                x_in=bleed_margin_x, y_in=bleed_margin_y,
            )
            out.append(RegionDescriptor(
                name="glue_zone_left", type="glue", rect=rect, face="outside",
                source="derived",
                note=f"{glue_w_in}\" along left opening edge",
            ))
        elif edge == "right_outer":
            rect = _to_pixel_rect(
                glue_w_in, trim_h, dpi,
                x_in=bleed_margin_x + trim_w - glue_w_in,
                y_in=bleed_margin_y,
            )
            out.append(RegionDescriptor(
                name="glue_zone_right", type="glue", rect=rect, face="outside",
                source="derived",
                note=f"{glue_w_in}\" along right opening edge",
            ))

    # An explicit glue rectangle (trifold) — emit alongside the opening-edge
    # strips, if present.
    explicit_glue = folding.get("glue_zone_dimensions_in")
    if explicit_glue:
        gw = float(explicit_glue.get("w", 0))
        gh = float(explicit_glue.get("h", 0))
        gx = float(explicit_glue.get("x_in_from_left", 0))
        anchor = explicit_glue.get("anchor", "bottom")
        gy = (
            bleed_margin_y + trim_h - gh
            if anchor == "bottom"
            else bleed_margin_y
        )
        rect = _to_pixel_rect(gw, gh, dpi, x_in=bleed_margin_x + gx, y_in=gy)
        out.append(RegionDescriptor(
            name="glue_zone_explicit", type="glue", rect=rect, face="outside",
            source="lob_help_center",
            note="Explicit glue rectangle (trifold).",
        ))

    # --- Fold gutters: 2*gutter_half_in band straddling each fold line. ---
    fold_xs = folding.get("fold_lines_in_from_left") or []
    fold_ys = folding.get("fold_lines_in_from_top") or []
    for i, fx_in in enumerate(sorted(float(x) for x in fold_xs)):
        rect = _to_pixel_rect(
            2 * gutter_half_in, trim_h, dpi,
            x_in=bleed_margin_x + fx_in - gutter_half_in,
            y_in=bleed_margin_y,
        )
        out.append(RegionDescriptor(
            name=f"fold_gutter_{i + 1}", type="fold_gutter", rect=rect,
            source="derived",
            note=f"Vertical fold at x={fx_in}\" ± {gutter_half_in}\"",
        ))
    for i, fy_in in enumerate(sorted(float(y) for y in fold_ys)):
        rect = _to_pixel_rect(
            trim_w, 2 * gutter_half_in, dpi,
            x_in=bleed_margin_x,
            y_in=bleed_margin_y + fy_in - gutter_half_in,
        )
        out.append(RegionDescriptor(
            name=f"fold_gutter_{i + 1}", type="fold_gutter", rect=rect,
            source="derived",
            note=f"Horizontal fold at y={fy_in}\" ± {gutter_half_in}\"",
        ))

    return out


# ---------------------------------------------------------------------------
# Face-zone derivation (postcards)
# ---------------------------------------------------------------------------


def _derive_face_zones_postcard(
    spec: MailerSpec,
    canvas: Rect,
    dpi: int,
) -> tuple[list[FaceDescriptor], list[RegionDescriptor]]:
    """Two faces (front, back), both occupying the full canvas. Face-local
    zones are read from spec.faces and resolved against the bleed dims."""
    bleed_w = spec.bleed_w_in or spec.trim_w_in
    bleed_h = spec.bleed_h_in or spec.trim_h_in
    safe_inset_in = spec.safe_inset_in or 0.0
    bleed_margin_x = (bleed_w - spec.trim_w_in) / 2.0
    bleed_margin_y = (bleed_h - spec.trim_h_in) / 2.0

    faces: list[FaceDescriptor] = []
    regions: list[RegionDescriptor] = []

    for face in (spec.faces or []):
        face_name = face["name"]
        face_rect = Rect(x=0, y=0, w=canvas.w, h=canvas.h)
        faces.append(FaceDescriptor(
            name=face_name,
            rect=face_rect,
            is_addressable=bool(face.get("is_addressable")),
        ))
        # face surface
        regions.append(RegionDescriptor(
            name=f"{face_name}_face", type="face", rect=face_rect,
            face=face_name, source="derived",
        ))
        # safe inset (face minus safe_inset on trim)
        safe = _to_pixel_rect(
            spec.trim_w_in - 2 * safe_inset_in,
            spec.trim_h_in - 2 * safe_inset_in,
            dpi,
            x_in=bleed_margin_x + safe_inset_in,
            y_in=bleed_margin_y + safe_inset_in,
        )
        regions.append(RegionDescriptor(
            name=f"{face_name}_safe", type="safe", rect=safe,
            face=face_name, derived_from=f"{face_name}_face",
            source="derived",
        ))
        # Per-face named zones
        for z in face.get("zones") or []:
            x_in, y_in, w_in, h_in = _resolve_rect_in(
                z["rect_in"], parent_w_in=bleed_w, parent_h_in=bleed_h,
            )
            rect = _to_pixel_rect(w_in, h_in, dpi, x_in=x_in, y_in=y_in)
            zone_name = z["name"]
            regions.append(RegionDescriptor(
                name=f"{face_name}_{zone_name}",
                type=z["type"],
                rect=rect,
                face=face_name,
                source=z.get("source"),
                aliases=list(z.get("aliases") or []),
                note=z.get("note") or z.get("rule"),
            ))

    return faces, regions


# ---------------------------------------------------------------------------
# Face-zone derivation (self-mailers)
# ---------------------------------------------------------------------------


def _derive_face_zones_self_mailer(
    spec: MailerSpec,
    canvas: Rect,
    dpi: int,
    panels_by_face: dict[str, dict[str, Rect]],
) -> tuple[list[FaceDescriptor], list[RegionDescriptor]]:
    """Outside + inside faces. Each face has its own panels (derived).
    Cover-panel zones are panel-local in spec.faces[].panel_zones."""
    bleed_w = spec.bleed_w_in or spec.trim_w_in
    bleed_h = spec.bleed_h_in or spec.trim_h_in
    safe_inset_in = spec.safe_inset_in or 0.0
    safe_inset_px = safe_inset_in * dpi

    faces: list[FaceDescriptor] = []
    regions: list[RegionDescriptor] = []

    for face in (spec.faces or []):
        face_name = face["name"]
        face_rect = Rect(x=0, y=0, w=canvas.w, h=canvas.h)
        faces.append(FaceDescriptor(
            name=face_name,
            rect=face_rect,
            is_addressable=bool(face.get("is_addressable")),
            cover_panel=face.get("cover_panel"),
        ))
        regions.append(RegionDescriptor(
            name=f"{face_name}_face", type="face", rect=face_rect,
            face=face_name, source="derived",
        ))

        # Panel rectangles for this face
        face_panels = panels_by_face.get(face_name) or {}
        for panel_name, panel_rect in face_panels.items():
            regions.append(RegionDescriptor(
                name=panel_name, type="panel", rect=panel_rect,
                face=face_name, source="derived",
            ))
            # Per-panel safe zone (panel minus safe_inset all sides)
            safe_rect = Rect(
                x=panel_rect.x + safe_inset_px,
                y=panel_rect.y + safe_inset_px,
                w=max(0.0, panel_rect.w - 2 * safe_inset_px),
                h=max(0.0, panel_rect.h - 2 * safe_inset_px),
            )
            regions.append(RegionDescriptor(
                name=f"{panel_name}_safe", type="safe", rect=safe_rect,
                face=face_name, panel=panel_name,
                derived_from=panel_name, source="derived",
            ))

        # Panel-local zones from `faces[].panel_zones`
        panel_zones = face.get("panel_zones") or {}
        for panel_name, zones_list in panel_zones.items():
            panel_rect = face_panels.get(panel_name)
            if panel_rect is None:
                continue
            panel_w_in = panel_rect.w / dpi
            panel_h_in = panel_rect.h / dpi
            panel_x_in = panel_rect.x / dpi
            panel_y_in = panel_rect.y / dpi
            for z in zones_list:
                x_in, y_in, w_in, h_in = _resolve_rect_in(
                    z["rect_in"], parent_w_in=panel_w_in, parent_h_in=panel_h_in,
                )
                rect = _to_pixel_rect(
                    w_in, h_in, dpi,
                    x_in=panel_x_in + x_in, y_in=panel_y_in + y_in,
                )
                zone_name = z["name"]
                # Namespace by face. Address window etc. are face-level
                # zones (one per face; their cover-panel-local position
                # is now resolved to canvas coords).
                regions.append(RegionDescriptor(
                    name=f"{face_name}_{zone_name}",
                    type=z["type"],
                    rect=rect,
                    face=face_name,
                    panel=panel_name,
                    source=z.get("source"),
                    aliases=list(z.get("aliases") or []),
                    note=z.get("note") or z.get("rule"),
                ))

    return faces, regions


# ---------------------------------------------------------------------------
# Top-level binder
# ---------------------------------------------------------------------------


def bind_spec_zones(spec: MailerSpec) -> SpecBinding:
    """Translate a MailerSpec's inches-based zones into pixel-space Rects.

    Origin: top-left of the bleed area (or trim area if no bleed).

    Emits both legacy zone names (for back-compat with the seeded
    hero-headline-postcard scaffold) and v2 face/panel-namespaced names
    (for the next-directive scaffold-authoring agent).
    """
    dpi = int(spec.production.get("required_dpi", 300))
    cw_in = spec.bleed_w_in if spec.bleed_w_in else spec.trim_w_in
    ch_in = spec.bleed_h_in if spec.bleed_h_in else spec.trim_h_in
    canvas = _to_pixel_rect(cw_in, ch_in, dpi)

    zones: dict[str, Rect] = {"canvas": canvas}
    regions: list[RegionDescriptor] = []
    faces: list[FaceDescriptor] = []

    # Trim zone (= canvas if no bleed; inset by 0.125" if there is bleed).
    if spec.bleed_w_in and spec.bleed_h_in:
        bleed_x_in = (spec.bleed_w_in - spec.trim_w_in) / 2
        bleed_y_in = (spec.bleed_h_in - spec.trim_h_in) / 2
        zones["trim"] = _to_pixel_rect(
            spec.trim_w_in, spec.trim_h_in, dpi, x_in=bleed_x_in, y_in=bleed_y_in
        )
    else:
        zones["trim"] = Rect(x=0, y=0, w=canvas.w, h=canvas.h)

    # Legacy safe zone (whole-trim minus safe_inset all sides). For postcards
    # this is now ALSO emitted as front_safe / back_safe; for self_mailers
    # it remains useful for non-folded constraints.
    if spec.safe_inset_in is not None:
        safe_inset = spec.safe_inset_in * dpi
        t = zones["trim"]
        zones["safe_zone"] = Rect(
            x=t.x + safe_inset,
            y=t.y + safe_inset,
            w=max(0, t.w - 2 * safe_inset),
            h=max(0, t.h - 2 * safe_inset),
        )

    # Legacy named zones from `zones` JSON. Kept verbatim so the seeded
    # hero-headline-postcard scaffold (which references `safe_zone`) and any
    # external reader that joined directly on `direct_mail_specs.zones` still
    # works after this migration.
    for name, z in (spec.zones or {}).items():
        if not isinstance(z, dict):
            continue
        rect = _zone_dict_to_rect(z, canvas, dpi)
        if rect is not None:
            zones[name] = rect

    # ----- v2 face/panel/region derivation -----
    panels_by_face: dict[str, dict[str, Rect]] = {}
    if spec.mailer_category == "postcard" and spec.faces:
        faces, regions = _derive_face_zones_postcard(spec, canvas, dpi)
    elif spec.mailer_category == "self_mailer" and spec.faces:
        panels_by_face = _derive_panels(spec, dpi)
        faces, panel_regions = _derive_face_zones_self_mailer(
            spec, canvas, dpi, panels_by_face,
        )
        regions.extend(panel_regions)
        regions.extend(_derive_glue_and_fold_zones(spec, dpi, panels_by_face))
    elif spec.mailer_category == "self_mailer" and spec.folding:
        # Future-format slot: trifold has folding metadata but no faces yet.
        # We still derive panels + glue + fold gutters so consumers see them.
        panels_by_face = _derive_panels(spec, dpi)
        for face_name, face_panels in panels_by_face.items():
            faces.append(FaceDescriptor(
                name=face_name,
                rect=Rect(x=0, y=0, w=canvas.w, h=canvas.h),
                is_addressable=face_name == "outside",
                cover_panel=(spec.folding or {}).get("cover_panel"),
            ))
            for panel_name, panel_rect in face_panels.items():
                regions.append(RegionDescriptor(
                    name=panel_name, type="panel", rect=panel_rect,
                    face=face_name, source="derived",
                ))
        regions.extend(_derive_glue_and_fold_zones(spec, dpi, panels_by_face))

    # Project regions into the flat zones map for the solver. Aliases get
    # added too so legacy DSL references (e.g. `safe_zone` for the seeded
    # scaffold, or `back_ink_free` from prior data) keep resolving.
    for r in regions:
        zones[r.name] = r.rect
        for alias in r.aliases:
            # Aliases are namespaced under the same face/panel as the region.
            if r.face:
                zones[f"{r.face}_{alias}"] = r.rect
            else:
                zones[alias] = r.rect

    return SpecBinding(
        spec=spec, dpi=dpi, canvas=canvas,
        zones=zones, regions=regions, faces=faces,
    )


# ---------------------------------------------------------------------------
# Marshalling for MCP / API consumers
# ---------------------------------------------------------------------------


def binding_to_dict(binding: SpecBinding) -> dict[str, Any]:
    """Plain-dict representation of a SpecBinding for MCP serialization."""
    return {
        "spec": {
            "id": binding.spec.id,
            "mailer_category": binding.spec.mailer_category,
            "variant": binding.spec.variant,
            "label": binding.spec.label,
            "bleed_w_in": binding.spec.bleed_w_in,
            "bleed_h_in": binding.spec.bleed_h_in,
            "trim_w_in": binding.spec.trim_w_in,
            "trim_h_in": binding.spec.trim_h_in,
            "safe_inset_in": binding.spec.safe_inset_in,
            "zones": binding.spec.zones,
            "folding": binding.spec.folding,
            "faces": binding.spec.faces,
            "production": binding.spec.production,
            "source_urls": binding.spec.source_urls,
            "template_pdf_url": binding.spec.template_pdf_url,
        },
        "dpi": binding.dpi,
        "canvas": binding.canvas.to_dict(),
        "zones": {n: r.to_dict() for n, r in binding.zones.items()},
        "regions": [
            {
                "name": r.name,
                "type": r.type,
                "rect": r.rect.to_dict(),
                "face": r.face,
                "panel": r.panel,
                "derived_from": r.derived_from,
                "source": r.source,
                "aliases": r.aliases,
                "note": r.note,
            }
            for r in binding.regions
        ],
        "faces": [
            {
                "name": f.name,
                "rect": f.rect.to_dict(),
                "is_addressable": f.is_addressable,
                "cover_panel": f.cover_panel,
            }
            for f in binding.faces
        ],
    }


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
