"""Scaffold brief schema + post-solve acceptance rule evaluator.

A `ScaffoldBrief` is the human-reviewable input the scaffold-authoring
agent works against. It captures (a) what scaffold to author (slug, format,
face, strategy, target specs), (b) the prose thesis the agent uses as a
north star, (c) the slot inventory the prop_schema must include, and
(d) strategy-specific structural rules (`acceptance_rules`) that must hold
on the resolved positions before the scaffold is persisted.

The acceptance-rule evaluator is deliberately small. Constraints that the
solver can express live in the DSL; this module only enforces the post-
resolve checks that don't naturally fit as DSL constraints — area
dominance, hierarchy ratios as a structural assertion (vs the soft size_ratio
constraint), required slot presence, slot-name patterns. It runs against
the same `positions` dict the solver returns from `validate_constraints`."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Acceptance rules — a small discriminated union of post-solve checks
# ---------------------------------------------------------------------------


class AreaDominanceRule(BaseModel):
    """`element` bbox area must be ≥ `min_ratio_vs_others` × every other
    element's area. Used by `offer` scaffolds where the offer block must
    visually dominate the rest of the layout."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["area_dominance"] = "area_dominance"
    element: str
    min_ratio_vs_others: float = Field(..., gt=0.0)


class SizeHierarchyRule(BaseModel):
    """`larger`'s height must be ≥ `min_ratio` × `smaller`'s height,
    measured on the resolved positions. Mirrors the DSL's `size_ratio`
    constraint but enforced as a hard post-create assertion: a scaffold
    that solves with `larger` only marginally bigger than `smaller`
    fails this brief even if the soft DSL constraint accepted it."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["size_hierarchy"] = "size_hierarchy"
    larger: str
    smaller: str
    min_ratio: float = Field(..., gt=0.0)


class SlotPresentRule(BaseModel):
    """Named slot must exist in the scaffold's `prop_schema.properties`.
    Used by `proof` (`proof_strip`) and `trust` (`credential_strip`) to
    guarantee the scaffold actually has the strategy-defining element."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["slot_present"] = "slot_present"
    slot: str


class MinSlotCountRule(BaseModel):
    """At least `min` slots in `prop_schema.properties` must have names
    starting with `category` (e.g., `proof_logo_1`, `proof_logo_2`,
    `proof_quote` all count for `category="proof"`). Used to enforce a
    lower bound on proof artifacts."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["min_slot_count"] = "min_slot_count"
    category: str = Field(..., min_length=1)
    min: int = Field(..., ge=1)


AcceptanceRule = Annotated[
    AreaDominanceRule | SizeHierarchyRule | SlotPresentRule | MinSlotCountRule,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


Strategy = Literal["hero", "proof", "offer", "trust"]
Face = Literal["front", "back", "outside", "inside"]
Format = Literal["postcard", "self_mailer"]


class CompatibleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    variant: str


class ScaffoldBrief(BaseModel):
    """The full brief the agent works from.

    Convention for `slug`: `<strategy>-<format>-<face>-<variant>` so a
    human reviewer can scan the file tree and immediately know which
    target each brief covers (e.g., `hero-postcard-front-6x9`).
    """

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(..., pattern=r"^[a-z0-9_-]+$", min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=256)
    strategy: Strategy
    face: Face
    format: Format
    compatible_specs: list[CompatibleSpec] = Field(..., min_length=1)
    thesis: str = Field(..., min_length=1)
    required_slots: list[str] = Field(default_factory=list)
    optional_slots: list[str] = Field(default_factory=list)
    acceptance_rules: list[AcceptanceRule] = Field(default_factory=list)
    placeholder_content: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Acceptance-rule evaluator
# ---------------------------------------------------------------------------


class AcceptanceFailure(BaseModel):
    """Structured failure for one acceptance rule. Surfaced to the
    authoring agent + the verification script."""

    model_config = ConfigDict(extra="forbid")
    rule_type: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


def _bbox_area(rect: dict[str, float]) -> float:
    return float(rect.get("w", 0.0)) * float(rect.get("h", 0.0))


def _bbox_height(rect: dict[str, float]) -> float:
    return float(rect.get("h", 0.0))


def evaluate_rules(
    rules: list[AcceptanceRule],
    *,
    positions: dict[str, dict[str, float]],
    prop_schema: dict[str, Any],
) -> list[AcceptanceFailure]:
    """Run every rule against the solver's `positions` (element → bbox)
    and the scaffold's `prop_schema`. Returns a list of failures; an
    empty list means the brief's structural acceptance bar is met."""
    failures: list[AcceptanceFailure] = []
    properties: dict[str, Any] = (prop_schema or {}).get("properties") or {}

    for rule in rules:
        if isinstance(rule, AreaDominanceRule):
            target = positions.get(rule.element)
            if target is None:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=f"area_dominance: element {rule.element!r} not in resolved positions",
                ))
                continue
            target_area = _bbox_area(target)
            for other_name, other_rect in positions.items():
                if other_name == rule.element:
                    continue
                other_area = _bbox_area(other_rect)
                if other_area <= 0:
                    continue
                ratio = target_area / other_area
                if ratio < rule.min_ratio_vs_others:
                    failures.append(AcceptanceFailure(
                        rule_type=rule.type,
                        message=(
                            f"area_dominance: {rule.element} area {target_area:.0f} is "
                            f"only {ratio:.2f}× {other_name}'s area {other_area:.0f}; "
                            f"required ≥ {rule.min_ratio_vs_others:.2f}×"
                        ),
                        detail={
                            "element": rule.element,
                            "competitor": other_name,
                            "ratio": round(ratio, 4),
                            "required": rule.min_ratio_vs_others,
                        },
                    ))
        elif isinstance(rule, SizeHierarchyRule):
            larger = positions.get(rule.larger)
            smaller = positions.get(rule.smaller)
            if larger is None or smaller is None:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=(
                        f"size_hierarchy: missing element(s) larger={rule.larger!r} "
                        f"smaller={rule.smaller!r}"
                    ),
                ))
                continue
            sh = _bbox_height(smaller)
            if sh <= 0:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=f"size_hierarchy: {rule.smaller} has zero height",
                ))
                continue
            lh = _bbox_height(larger)
            ratio = lh / sh
            if ratio < rule.min_ratio:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=(
                        f"size_hierarchy: {rule.larger}.h {lh:.1f} is only {ratio:.2f}× "
                        f"{rule.smaller}.h {sh:.1f}; required ≥ {rule.min_ratio:.2f}×"
                    ),
                    detail={
                        "larger": rule.larger,
                        "smaller": rule.smaller,
                        "ratio": round(ratio, 4),
                        "required": rule.min_ratio,
                    },
                ))
        elif isinstance(rule, SlotPresentRule):
            if rule.slot not in properties:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=f"slot_present: {rule.slot!r} missing from prop_schema.properties",
                    detail={"slot": rule.slot},
                ))
        elif isinstance(rule, MinSlotCountRule):
            count = sum(1 for name in properties if name.startswith(rule.category))
            if count < rule.min:
                failures.append(AcceptanceFailure(
                    rule_type=rule.type,
                    message=(
                        f"min_slot_count: only {count} slot(s) in prop_schema.properties "
                        f"start with {rule.category!r}; required ≥ {rule.min}"
                    ),
                    detail={
                        "category": rule.category,
                        "found": count,
                        "required": rule.min,
                    },
                ))

    return failures
