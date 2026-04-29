"""Constraint-specification DSL.

This is the structured layout language scaffolds are authored in. It is
deliberately a JSON-serializable Pydantic schema (not raw kiwisolver calls)
so that:

  * The same scaffold is portable across the Python server and a future
    TypeScript browser bundle (which will use @lume/kiwi).
  * LLM scaffold-authoring agents emit structured JSON, which is far easier
    to validate, diff, and version than imperative solver calls.
  * Constraint conflicts can be reported with the original DSL term — not a
    cryptic "row 47 of the kiwi tableau is unsatisfiable".

The solver in app/dmaas/solver.py translates these models to kiwisolver
constraints. Anything that can't be expressed as a linear constraint
(no_overlap, color_contrast, grid_align) runs as a post-solve validator.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

# kiwisolver native strengths. Wired through verbatim so authors can express
# "this rule is required" vs "this rule is a soft preference".
Strength = Literal["required", "strong", "medium", "weak"]
Anchor = Literal[
    "top_left",
    "top_center",
    "top_right",
    "middle_left",
    "center",
    "middle_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
]
HorizontalAlignment = Literal["left", "center", "right"]
VerticalAlignment = Literal["top", "middle", "bottom"]


class _ConstraintBase(BaseModel):
    """Common fields. Every constraint has a `type` discriminator + strength."""

    model_config = ConfigDict(extra="forbid")
    type: str
    strength: Strength = "required"


# ---------------------------------------------------------------------------
# Layout constraints (kiwi-solvable)
# ---------------------------------------------------------------------------


class InsideConstraint(_ConstraintBase):
    """Element bbox must be fully inside zone bbox."""

    type: Literal["inside"] = "inside"
    element: str
    zone: str


class VerticalGapConstraint(_ConstraintBase):
    """Vertical spacing between two stacked elements.

    `min` is enforced as a hard inequality at `strength`. `preferred`, when
    set, is added as an equality at "weak" strength so the solver tends
    toward the preferred gap when there's slack."""

    type: Literal["vertical_gap"] = "vertical_gap"
    above: str
    below: str
    min: float = 0.0
    preferred: float | None = None


class HorizontalGapConstraint(_ConstraintBase):
    type: Literal["horizontal_gap"] = "horizontal_gap"
    left: str
    right: str
    min: float = 0.0
    preferred: float | None = None


class MinSizeConstraint(_ConstraintBase):
    type: Literal["min_size"] = "min_size"
    element: str
    min_width: float | None = None
    min_height: float | None = None


class MaxSizeConstraint(_ConstraintBase):
    type: Literal["max_size"] = "max_size"
    element: str
    max_width: float | None = None
    max_height: float | None = None


class MaxWidthPercentOfZoneConstraint(_ConstraintBase):
    type: Literal["max_width_percent_of_zone"] = "max_width_percent_of_zone"
    element: str
    zone: str
    percent: float = Field(..., gt=0.0, le=100.0)


class MaxHeightPercentOfZoneConstraint(_ConstraintBase):
    type: Literal["max_height_percent_of_zone"] = "max_height_percent_of_zone"
    element: str
    zone: str
    percent: float = Field(..., gt=0.0, le=100.0)


class HorizontalAlignConstraint(_ConstraintBase):
    """Align element horizontally to a reference (zone or element)."""

    type: Literal["horizontal_align"] = "horizontal_align"
    element: str
    align: HorizontalAlignment
    reference: str


class VerticalAlignConstraint(_ConstraintBase):
    type: Literal["vertical_align"] = "vertical_align"
    element: str
    align: VerticalAlignment
    reference: str


class AnchorConstraint(_ConstraintBase):
    """Pin an element to a specific corner / edge of a reference, with margin."""

    type: Literal["anchor"] = "anchor"
    element: str
    position: Anchor
    reference: str
    margin: float = 0.0


class SizeRatioConstraint(_ConstraintBase):
    """Hierarchy: `larger` is at least `min_ratio` × `smaller`.

    Both arguments are dotted accessors like `headline.height` —
    `<element>.<width|height>`. The solver dispatches to the right variable.
    """

    type: Literal["size_ratio"] = "size_ratio"
    larger: str
    smaller: str
    min_ratio: float = Field(..., gt=0.0)

    @field_validator("larger", "smaller")
    @classmethod
    def _validate_accessor(cls, v: str) -> str:
        if "." not in v:
            raise ValueError("size_ratio operands must be '<element>.<width|height>'")
        _, prop = v.rsplit(".", 1)
        if prop not in {"width", "height"}:
            raise ValueError(f"size_ratio property must be 'width' or 'height', got {prop!r}")
        return v


# ---------------------------------------------------------------------------
# Validator constraints (post-solve, not kiwi-expressible)
# ---------------------------------------------------------------------------


class NoOverlapConstraint(_ConstraintBase):
    """List of elements must not overlap pairwise. Cassowary cannot express
    disjunctive non-overlap natively — this runs as a post-solve check."""

    type: Literal["no_overlap"] = "no_overlap"
    elements: list[str] = Field(..., min_length=2)


class NoOverlapWithZoneConstraint(_ConstraintBase):
    type: Literal["no_overlap_with_zone"] = "no_overlap_with_zone"
    element: str
    zone: str


class ColorContrastConstraint(_ConstraintBase):
    """WCAG contrast ratio between two color references in content_config.

    Operands are dotted paths into content_config — e.g. `headline.color`
    must be hex strings. Validator computes WCAG AA/AAA ratio."""

    type: Literal["color_contrast"] = "color_contrast"
    foreground: str
    background: str
    min_ratio: float = Field(..., gt=0.0)


class GridAlignConstraint(_ConstraintBase):
    """Element x/y must be on a grid increment. Implemented as post-solve
    rounding tolerance check."""

    type: Literal["grid_align"] = "grid_align"
    element: str
    grid: float = Field(..., gt=0.0)
    axis: Literal["x", "y", "both"] = "both"


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

Constraint = Annotated[
    InsideConstraint
    | VerticalGapConstraint
    | HorizontalGapConstraint
    | MinSizeConstraint
    | MaxSizeConstraint
    | MaxWidthPercentOfZoneConstraint
    | MaxHeightPercentOfZoneConstraint
    | HorizontalAlignConstraint
    | VerticalAlignConstraint
    | AnchorConstraint
    | SizeRatioConstraint
    | NoOverlapConstraint
    | NoOverlapWithZoneConstraint
    | ColorContrastConstraint
    | GridAlignConstraint,
    Field(discriminator="type"),
]

# Set of every constraint type the solver implements. Used to route to
# linear vs validator phase and to surface a "supported" list to the LLM.
LINEAR_CONSTRAINT_TYPES: frozenset[str] = frozenset(
    {
        "inside",
        "vertical_gap",
        "horizontal_gap",
        "min_size",
        "max_size",
        "max_width_percent_of_zone",
        "max_height_percent_of_zone",
        "horizontal_align",
        "vertical_align",
        "anchor",
        "size_ratio",
    }
)
VALIDATOR_CONSTRAINT_TYPES: frozenset[str] = frozenset(
    {
        "no_overlap",
        "no_overlap_with_zone",
        "color_contrast",
        "grid_align",
    }
)
ALL_CONSTRAINT_TYPES: frozenset[str] = LINEAR_CONSTRAINT_TYPES | VALIDATOR_CONSTRAINT_TYPES

# Faces of a piece of direct mail. Zones in direct_mail_specs.zones are
# face-namespaced via prefix (`front_*`, `back_*`, `outside_*`, `inside_*`)
# so a scaffold can declare its face once and the DSL will reject zone
# references from any other face at parse time.
Face = Literal["front", "back", "outside", "inside"]
FACE_PREFIXES: dict[str, str] = {
    "front": "front_",
    "back": "back_",
    "outside": "outside_",
    "inside": "inside_",
}

# Legacy face-agnostic zones — accepted under any declared `face`. These
# predate the v2 face-namespaced catalog and remain valid for single-face
# specs (e.g. simple postcards seeded before v2).
FACE_AGNOSTIC_ZONES: frozenset[str] = frozenset({"safe_zone", "canvas", "trim"})


# ---------------------------------------------------------------------------
# Top-level constraint specification
# ---------------------------------------------------------------------------


class ConstraintSpecification(BaseModel):
    """Full layout description for a scaffold.

    `elements` is the closed set of element names the constraints can refer
    to (validated on parse). `zones` is the closed set of zone names the
    spec brings in from direct_mail_specs.zones (validated when the solver
    binds the spec).
    """

    model_config = ConfigDict(extra="forbid")

    elements: list[str] = Field(..., min_length=1)
    zones: list[str] = Field(default_factory=list)
    constraints: list[Constraint]
    face: Face | None = None

    @field_validator("elements")
    @classmethod
    def _unique_elements(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("element names must be unique")
        return v

    def referenced_elements(self) -> set[str]:
        """Every element name referenced by any constraint."""
        names: set[str] = set()
        for c in self.constraints:
            names.update(_constraint_element_refs(c))
        return names

    def referenced_zones(self) -> set[str]:
        names: set[str] = set()
        for c in self.constraints:
            names.update(_constraint_zone_refs(c))
        return names

    def validate_references(self) -> list[str]:
        """Returns a list of error strings if any constraint references an
        unknown element / zone. Empty list = OK."""
        errs: list[str] = []
        elem_set = set(self.elements)
        zone_set = set(self.zones)
        for idx, c in enumerate(self.constraints):
            for name in _constraint_element_refs(c):
                if name not in elem_set:
                    errs.append(
                        f"constraints[{idx}] ({c.type}) references unknown element {name!r}"
                    )
            for name in _constraint_zone_refs(c):
                if name not in zone_set:
                    errs.append(f"constraints[{idx}] ({c.type}) references unknown zone {name!r}")
        if self.face is not None:
            prefix = FACE_PREFIXES[self.face]
            for idx, c in enumerate(self.constraints):
                for name in _constraint_zone_refs(c):
                    if name in FACE_AGNOSTIC_ZONES:
                        continue
                    if not name.startswith(prefix):
                        errs.append(
                            f"constraints[{idx}] ({c.type}) references zone {name!r} "
                            f"which does not belong to face {self.face!r}"
                        )
        return errs


def _constraint_element_refs(c: Any) -> list[str]:
    """Element names a constraint refers to."""
    t = c.type
    if t in ("inside", "min_size", "max_size", "max_width_percent_of_zone",
             "max_height_percent_of_zone", "anchor", "no_overlap_with_zone",
             "grid_align"):
        return [c.element]
    if t == "vertical_gap":
        return [c.above, c.below]
    if t == "horizontal_gap":
        return [c.left, c.right]
    if t in ("horizontal_align", "vertical_align"):
        # `reference` may be an element or a zone — resolve via membership at
        # bind time, not here. Only `element` is guaranteed-element.
        return [c.element]
    if t == "size_ratio":
        return [c.larger.split(".", 1)[0], c.smaller.split(".", 1)[0]]
    if t == "no_overlap":
        return list(c.elements)
    if t == "color_contrast":
        # foreground/background are dotted paths into content_config — first
        # segment is an element name.
        return [c.foreground.split(".", 1)[0], c.background.split(".", 1)[0]]
    return []


def _constraint_zone_refs(c: Any) -> list[str]:
    t = c.type
    if t in ("inside", "max_width_percent_of_zone",
             "max_height_percent_of_zone", "no_overlap_with_zone"):
        return [c.zone]
    if t == "anchor":
        # `reference` for anchor can be either zone or element. Try-zone first.
        return [c.reference]
    if t in ("horizontal_align", "vertical_align"):
        return [c.reference]
    return []
