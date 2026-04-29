"""Pure constraint solver.

Inputs → outputs, no I/O. Server-side `solve()` is the same function the
authoring agent uses to test a proposed constraint spec, the same function
`POST /designs/:id/validate` uses to pre-flight a design, and the same
function `POST /designs` uses to compute resolved positions on save.

A future browser-side TypeScript module will be a port of this same logic
against @lume/kiwi, consuming the same DSL JSON.

Two phases:
  1. **Linear phase** — kiwisolver. Builds Variables for every element's
     (x, y, w, h), wires the linear-expressible DSL constraints, and solves.
  2. **Validator phase** — runs over the linear-phase output. Handles the
     non-linear DSL terms (no_overlap, color_contrast, grid_align).

Failure mode: when the linear phase is unsatisfiable, we return a
`SolveFailure` enumerating every constraint we *tried to add* — caller
narrows from there. (kiwisolver itself raises on the offending term, so
we accumulate up to that point and return the failure list.)

Determinism: kiwisolver's add_edit_variable() ties resolve based on
suggestion strength + insertion order. Identical inputs (in dict order)
produce identical outputs. Tested under `test_dmaas_solver_deterministic`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import kiwisolver as kiwi

from app.dmaas.dsl import (
    LINEAR_CONSTRAINT_TYPES,
    VALIDATOR_CONSTRAINT_TYPES,
    AnchorConstraint,
    ColorContrastConstraint,
    Constraint,
    ConstraintSpecification,
    GridAlignConstraint,
    HorizontalAlignConstraint,
    HorizontalGapConstraint,
    InsideConstraint,
    MaxHeightPercentOfZoneConstraint,
    MaxSizeConstraint,
    MaxWidthPercentOfZoneConstraint,
    MinSizeConstraint,
    NoOverlapConstraint,
    NoOverlapWithZoneConstraint,
    SizeRatioConstraint,
    Strength,
    VerticalAlignConstraint,
    VerticalGapConstraint,
)

# ---------------------------------------------------------------------------
# Public API types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in pixel coordinates (top-left origin)."""

    x: float
    y: float
    w: float
    h: float

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class ElementIntrinsics:
    """Caller-supplied size hints for an element. Text elements have
    intrinsic dimensions derived from font metrics; the solver doesn't do
    typography — the caller does, then hands us the bounds.

    `min_*` are hard floors, `max_*` are hard ceilings, `preferred_*` are
    soft hints used to break solver indeterminacy."""

    min_width: float = 0.0
    min_height: float = 0.0
    max_width: float | None = None
    max_height: float | None = None
    preferred_width: float | None = None
    preferred_height: float | None = None


@dataclass
class ConstraintConflict:
    """A constraint we couldn't satisfy. `phase` is "linear" or "validator"."""

    constraint_index: int
    constraint_type: str
    phase: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SolveResult:
    is_valid: bool
    positions: dict[str, Rect]  # element name → resolved Rect
    conflicts: list[ConstraintConflict]
    warnings: list[str] = field(default_factory=list)

    def positions_dict(self) -> dict[str, dict[str, float]]:
        return {name: r.to_dict() for name, r in self.positions.items()}


# ---------------------------------------------------------------------------
# Strength translation
# ---------------------------------------------------------------------------


def _kiwi_strength(s: Strength) -> float:
    return {
        "required": kiwi.strength.required,
        "strong": kiwi.strength.strong,
        "medium": kiwi.strength.medium,
        "weak": kiwi.strength.weak,
    }[s]


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def solve(
    spec: ConstraintSpecification,
    *,
    zones: dict[str, Rect],
    intrinsics: dict[str, ElementIntrinsics] | None = None,
    content: dict[str, Any] | None = None,
) -> SolveResult:
    """Run the constraint spec against the given zones + intrinsics.

    Args:
        spec: validated DSL.
        zones: name → absolute Rect in pixels (top-left origin).
        intrinsics: per-element min/max/preferred sizes. Missing entries
            default to ElementIntrinsics() (no bounds, no preferences) —
            useful for visualization but the solver may produce zero-sized
            elements without a hint.
        content: per-element content dict. Used by validator phase for
            color_contrast etc. Element name → dict of content fields.
    """
    intrinsics = intrinsics or {}
    content = content or {}

    # Sanity check: spec references must resolve. We surface unresolved refs
    # as conflicts up front rather than crashing inside the solver.
    ref_errors = spec.validate_references()
    if ref_errors:
        return SolveResult(
            is_valid=False,
            positions={},
            conflicts=[
                ConstraintConflict(
                    constraint_index=-1,
                    constraint_type="reference",
                    phase="prevalidate",
                    message=msg,
                )
                for msg in ref_errors
            ],
        )

    solver = kiwi.Solver()

    # 1 variable per (element, dim).
    elem_vars: dict[str, dict[str, kiwi.Variable]] = {
        name: {
            "x": kiwi.Variable(f"{name}_x"),
            "y": kiwi.Variable(f"{name}_y"),
            "w": kiwi.Variable(f"{name}_w"),
            "h": kiwi.Variable(f"{name}_h"),
        }
        for name in spec.elements
    }

    # Sanity floors: width/height ≥ 0 always.
    for name in spec.elements:
        v = elem_vars[name]
        solver.addConstraint((v["w"] >= 0) | "required")
        solver.addConstraint((v["h"] >= 0) | "required")

    # Apply intrinsics as constraints, then add weak "preferred size" edits
    # so the solver has something to optimize toward.
    for name in spec.elements:
        intr = intrinsics.get(name, ElementIntrinsics())
        v = elem_vars[name]
        if intr.min_width > 0:
            solver.addConstraint((v["w"] >= intr.min_width) | "required")
        if intr.min_height > 0:
            solver.addConstraint((v["h"] >= intr.min_height) | "required")
        if intr.max_width is not None:
            solver.addConstraint((v["w"] <= intr.max_width) | "required")
        if intr.max_height is not None:
            solver.addConstraint((v["h"] <= intr.max_height) | "required")
        # Preferred sizes are caller-supplied content-derived dimensions
        # (text rendered length, image natural size). They should win over
        # the DSL's `preferred` gap hints (which are also soft). "medium"
        # gives them headroom over weak gap preferences without overriding
        # explicit min/max constraints.
        if intr.preferred_width is not None:
            solver.addConstraint((v["w"] == intr.preferred_width) | "medium")
        if intr.preferred_height is not None:
            solver.addConstraint((v["h"] == intr.preferred_height) | "medium")

    # Stable layout heuristic: elements have an *ultra*-weak preference to
    # be at the zone origin. Without this, kiwi may park unconstrained
    # elements at (0, 0) outside any visible zone, which makes failure modes
    # confusing. Strength is below "weak" so explicit anchors / preferred
    # sizes dominate every time.
    _ULTRAWEAK = kiwi.strength.create(0, 0, 1)
    if zones:
        first_zone = next(iter(zones.values()))
        for name in spec.elements:
            v = elem_vars[name]
            solver.addConstraint((v["x"] == first_zone.x) | _ULTRAWEAK)
            solver.addConstraint((v["y"] == first_zone.y) | _ULTRAWEAK)

    # ----- Linear phase -----
    conflicts: list[ConstraintConflict] = []
    linear_constraints = [
        (i, c) for i, c in enumerate(spec.constraints) if c.type in LINEAR_CONSTRAINT_TYPES
    ]
    for idx, c in linear_constraints:
        try:
            for kc in _to_kiwi(c, elem_vars, zones):
                solver.addConstraint(kc)
        except kiwi.UnsatisfiableConstraint as e:
            conflicts.append(
                ConstraintConflict(
                    constraint_index=idx,
                    constraint_type=c.type,
                    phase="linear",
                    message=f"unsatisfiable: {e}",
                    detail=c.model_dump(),
                )
            )
        except (kiwi.BadRequiredStrength, ValueError) as e:
            conflicts.append(
                ConstraintConflict(
                    constraint_index=idx,
                    constraint_type=c.type,
                    phase="linear",
                    message=str(e),
                    detail=c.model_dump(),
                )
            )

    if conflicts:
        # Don't run the solver — the constraint set is broken.
        return SolveResult(is_valid=False, positions={}, conflicts=conflicts)

    solver.updateVariables()

    positions = {
        name: Rect(
            x=round(v["x"].value(), 4),
            y=round(v["y"].value(), 4),
            w=round(v["w"].value(), 4),
            h=round(v["h"].value(), 4),
        )
        for name, v in elem_vars.items()
    }

    # ----- Validator phase -----
    validator_conflicts = _run_validators(spec, positions, zones, content)

    return SolveResult(
        is_valid=not validator_conflicts,
        positions=positions,
        conflicts=validator_conflicts,
    )


# ---------------------------------------------------------------------------
# DSL → kiwi translation
# ---------------------------------------------------------------------------


def _to_kiwi(
    c: Constraint,
    elem_vars: dict[str, dict[str, kiwi.Variable]],
    zones: dict[str, Rect],
) -> list[Any]:
    """Return the list of kiwisolver Constraint objects implementing `c`."""
    s = c.strength
    out: list[Any] = []

    def E(name: str, dim: str) -> kiwi.Variable:
        return elem_vars[name][dim]

    if isinstance(c, InsideConstraint):
        z = zones[c.zone]
        out.append(((E(c.element, "x") >= z.x)) | s)
        out.append(((E(c.element, "y") >= z.y)) | s)
        out.append(((E(c.element, "x") + E(c.element, "w") <= z.x + z.w)) | s)
        out.append(((E(c.element, "y") + E(c.element, "h") <= z.y + z.h)) | s)
        return out

    if isinstance(c, VerticalGapConstraint):
        out.append(((E(c.below, "y") >= E(c.above, "y") + E(c.above, "h") + c.min)) | s)
        if c.preferred is not None:
            out.append(
                ((E(c.below, "y") - E(c.above, "y") - E(c.above, "h") == c.preferred)) | "weak"
            )
        return out

    if isinstance(c, HorizontalGapConstraint):
        out.append(((E(c.right, "x") >= E(c.left, "x") + E(c.left, "w") + c.min)) | s)
        if c.preferred is not None:
            out.append(
                ((E(c.right, "x") - E(c.left, "x") - E(c.left, "w") == c.preferred)) | "weak"
            )
        return out

    if isinstance(c, MinSizeConstraint):
        if c.min_width is not None:
            out.append(((E(c.element, "w") >= c.min_width)) | s)
        if c.min_height is not None:
            out.append(((E(c.element, "h") >= c.min_height)) | s)
        return out

    if isinstance(c, MaxSizeConstraint):
        if c.max_width is not None:
            out.append(((E(c.element, "w") <= c.max_width)) | s)
        if c.max_height is not None:
            out.append(((E(c.element, "h") <= c.max_height)) | s)
        return out

    if isinstance(c, MaxWidthPercentOfZoneConstraint):
        z = zones[c.zone]
        out.append(((E(c.element, "w") <= z.w * (c.percent / 100.0))) | s)
        return out

    if isinstance(c, MaxHeightPercentOfZoneConstraint):
        z = zones[c.zone]
        out.append(((E(c.element, "h") <= z.h * (c.percent / 100.0))) | s)
        return out

    if isinstance(c, HorizontalAlignConstraint):
        rx, rw = _ref_x_w(c.reference, elem_vars, zones)
        ex, ew = E(c.element, "x"), E(c.element, "w")
        if c.align == "left":
            out.append(((ex == rx)) | s)
        elif c.align == "right":
            out.append(((ex + ew == rx + rw)) | s)
        elif c.align == "center":
            # ex + ew/2 == rx + rw/2 → 2*ex + ew == 2*rx + rw
            out.append(((2 * ex + ew == 2 * rx + rw)) | s)
        return out

    if isinstance(c, VerticalAlignConstraint):
        ry, rh = _ref_y_h(c.reference, elem_vars, zones)
        ey, eh = E(c.element, "y"), E(c.element, "h")
        if c.align == "top":
            out.append(((ey == ry)) | s)
        elif c.align == "bottom":
            out.append(((ey + eh == ry + rh)) | s)
        elif c.align == "middle":
            out.append(((2 * ey + eh == 2 * ry + rh)) | s)
        return out

    if isinstance(c, AnchorConstraint):
        rx, rw = _ref_x_w(c.reference, elem_vars, zones)
        ry, rh = _ref_y_h(c.reference, elem_vars, zones)
        ex, ey, ew, eh = (E(c.element, d) for d in ("x", "y", "w", "h"))
        m = c.margin
        # Compose: each anchor is two equalities (one per axis).
        if c.position in ("top_left", "top_center", "top_right"):
            out.append(((ey == ry + m)) | s)
        if c.position in ("middle_left", "center", "middle_right"):
            out.append(((2 * ey + eh == 2 * ry + rh)) | s)
        if c.position in ("bottom_left", "bottom_center", "bottom_right"):
            out.append(((ey + eh == ry + rh - m)) | s)
        if c.position in ("top_left", "middle_left", "bottom_left"):
            out.append(((ex == rx + m)) | s)
        if c.position in ("top_center", "center", "bottom_center"):
            out.append(((2 * ex + ew == 2 * rx + rw)) | s)
        if c.position in ("top_right", "middle_right", "bottom_right"):
            out.append(((ex + ew == rx + rw - m)) | s)
        return out

    if isinstance(c, SizeRatioConstraint):
        l_elem, l_dim = c.larger.rsplit(".", 1)
        s_elem, s_dim = c.smaller.rsplit(".", 1)
        # width → "w", height → "h"
        l_v = E(l_elem, "w" if l_dim == "width" else "h")
        s_v = E(s_elem, "w" if s_dim == "width" else "h")
        out.append(((l_v >= s_v * c.min_ratio)) | s)
        return out

    raise ValueError(f"_to_kiwi: unsupported constraint type {c.type}")


def _ref_x_w(
    ref: str,
    elem_vars: dict[str, dict[str, kiwi.Variable]],
    zones: dict[str, Rect],
) -> tuple[Any, Any]:
    if ref in zones:
        return zones[ref].x, zones[ref].w
    if ref in elem_vars:
        return elem_vars[ref]["x"], elem_vars[ref]["w"]
    raise ValueError(f"unknown reference {ref!r}")


def _ref_y_h(
    ref: str,
    elem_vars: dict[str, dict[str, kiwi.Variable]],
    zones: dict[str, Rect],
) -> tuple[Any, Any]:
    if ref in zones:
        return zones[ref].y, zones[ref].h
    if ref in elem_vars:
        return elem_vars[ref]["y"], elem_vars[ref]["h"]
    raise ValueError(f"unknown reference {ref!r}")


# ---------------------------------------------------------------------------
# Validator phase (post-solve)
# ---------------------------------------------------------------------------


def _run_validators(
    spec: ConstraintSpecification,
    positions: dict[str, Rect],
    zones: dict[str, Rect],
    content: dict[str, Any],
) -> list[ConstraintConflict]:
    out: list[ConstraintConflict] = []
    for idx, c in enumerate(spec.constraints):
        if c.type not in VALIDATOR_CONSTRAINT_TYPES:
            continue
        if isinstance(c, NoOverlapConstraint):
            for i in range(len(c.elements)):
                for j in range(i + 1, len(c.elements)):
                    a, b = c.elements[i], c.elements[j]
                    if _rects_overlap(positions[a], positions[b]):
                        out.append(
                            ConstraintConflict(
                                constraint_index=idx,
                                constraint_type=c.type,
                                phase="validator",
                                message=f"elements {a!r} and {b!r} overlap",
                                detail={
                                    "a": positions[a].to_dict(),
                                    "b": positions[b].to_dict(),
                                },
                            )
                        )
        elif isinstance(c, NoOverlapWithZoneConstraint):
            elem = positions[c.element]
            zone = zones[c.zone]
            zone_rect = Rect(zone.x, zone.y, zone.w, zone.h)
            if _rects_overlap(elem, zone_rect):
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=f"element {c.element!r} overlaps zone {c.zone!r}",
                        detail={"element": elem.to_dict(), "zone": zone_rect.to_dict()},
                    )
                )
        elif isinstance(c, ColorContrastConstraint):
            fg = _resolve_dotted(content, c.foreground)
            bg = _resolve_dotted(content, c.background)
            if not (isinstance(fg, str) and isinstance(bg, str)):
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=f"could not resolve colors for {c.foreground} or {c.background}",
                    )
                )
                continue
            try:
                ratio = wcag_contrast_ratio(fg, bg)
            except ValueError as e:
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=f"invalid color: {e}",
                    )
                )
                continue
            if ratio < c.min_ratio:
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=(
                            f"contrast ratio {ratio:.2f}:1 below minimum {c.min_ratio}:1 "
                            f"({c.foreground}={fg} vs {c.background}={bg})"
                        ),
                        detail={"ratio": ratio, "foreground": fg, "background": bg},
                    )
                )
        elif isinstance(c, GridAlignConstraint):
            elem = positions[c.element]
            if c.axis in ("x", "both") and not _on_grid(elem.x, c.grid):
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=f"element {c.element!r} x={elem.x} not on grid {c.grid}",
                    )
                )
            if c.axis in ("y", "both") and not _on_grid(elem.y, c.grid):
                out.append(
                    ConstraintConflict(
                        constraint_index=idx,
                        constraint_type=c.type,
                        phase="validator",
                        message=f"element {c.element!r} y={elem.y} not on grid {c.grid}",
                    )
                )
    return out


def _rects_overlap(a: Rect, b: Rect, eps: float = 0.5) -> bool:
    """0.5px tolerance — kiwisolver outputs floats, exact boundaries are
    not actually overlap."""
    return not (
        a.x + a.w <= b.x + eps
        or b.x + b.w <= a.x + eps
        or a.y + a.h <= b.y + eps
        or b.y + b.h <= a.y + eps
    )


def _on_grid(v: float, grid: float, eps: float = 0.5) -> bool:
    rem = math.fmod(v, grid)
    return rem <= eps or grid - rem <= eps


def _resolve_dotted(content: dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    cur: Any = content
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


# ---------------------------------------------------------------------------
# WCAG contrast (relative luminance, sRGB → linear, ratio formula)
# ---------------------------------------------------------------------------


def wcag_contrast_ratio(hex_a: str, hex_b: str) -> float:
    la = _relative_luminance(_hex_to_rgb(hex_a))
    lb = _relative_luminance(_hex_to_rgb(hex_b))
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    s = hex_color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"bad hex color {hex_color!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        f = c / 255.0
        return f / 12.92 if f <= 0.03928 else ((f + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)
