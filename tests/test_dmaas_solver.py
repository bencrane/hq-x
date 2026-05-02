"""Pure-function tests for the DMaaS solver.

These tests don't touch the DB or FastAPI. They exercise the solver
against synthetic scaffolds + zones to verify:
  * Linear constraints translate correctly
  * Determinism (same inputs → same outputs)
  * Validator-phase failures (no_overlap, color_contrast, grid_align)
  * Linear-phase unsatisfiability returns structured conflicts
"""

from __future__ import annotations

import pytest

from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.solver import (
    ElementIntrinsics,
    Rect,
    solve,
    wcag_contrast_ratio,
)


def _safe_zone() -> dict[str, Rect]:
    return {"safe_zone": Rect(x=20, y=20, w=1700, h=2700)}


def _hero_headline_spec() -> ConstraintSpecification:
    return ConstraintSpecification.model_validate(
        {
            "elements": ["headline", "subhead", "cta"],
            "zones": ["safe_zone"],
            "constraints": [
                {"type": "inside", "element": "headline", "zone": "safe_zone"},
                {"type": "inside", "element": "subhead", "zone": "safe_zone"},
                {"type": "inside", "element": "cta", "zone": "safe_zone"},
                {
                    "type": "anchor",
                    "element": "headline",
                    "position": "top_center",
                    "reference": "safe_zone",
                    "margin": 80,
                },
                {
                    "type": "anchor",
                    "element": "cta",
                    "position": "bottom_center",
                    "reference": "safe_zone",
                    "margin": 100,
                },
                {
                    "type": "horizontal_align",
                    "element": "subhead",
                    "align": "center",
                    "reference": "safe_zone",
                    "strength": "strong",
                },
                {
                    "type": "vertical_gap",
                    "above": "headline",
                    "below": "subhead",
                    "min": 24,
                    "preferred": 40,
                    "strength": "strong",
                },
                {
                    "type": "size_ratio",
                    "larger": "headline.height",
                    "smaller": "subhead.height",
                    "min_ratio": 1.5,
                    "strength": "strong",
                },
                {
                    "type": "no_overlap",
                    "elements": ["headline", "subhead", "cta"],
                },
            ],
        }
    )


def _hero_intrinsics() -> dict[str, ElementIntrinsics]:
    return {
        "headline": ElementIntrinsics(
            min_width=400, max_width=1500, preferred_width=1200, preferred_height=140
        ),
        "subhead": ElementIntrinsics(
            min_width=400, max_width=1300, preferred_width=1000, preferred_height=70
        ),
        "cta": ElementIntrinsics(
            min_width=400, max_width=800, preferred_width=600, preferred_height=80
        ),
    }


def test_solver_produces_positions_within_safe_zone():
    spec = _hero_headline_spec()
    zones = _safe_zone()
    result = solve(spec, zones=zones, intrinsics=_hero_intrinsics())
    assert result.is_valid
    z = zones["safe_zone"]
    for name, r in result.positions.items():
        assert r.x >= z.x - 0.5, f"{name} left of zone"
        assert r.y >= z.y - 0.5, f"{name} above zone"
        assert r.x + r.w <= z.x + z.w + 0.5, f"{name} right of zone"
        assert r.y + r.h <= z.y + z.h + 0.5, f"{name} below zone"


def test_solver_anchors_top_and_bottom():
    spec = _hero_headline_spec()
    zones = _safe_zone()
    result = solve(spec, zones=zones, intrinsics=_hero_intrinsics())
    z = zones["safe_zone"]
    headline = result.positions["headline"]
    cta = result.positions["cta"]
    assert headline.y == pytest.approx(z.y + 80, abs=0.5)  # top anchor margin
    assert cta.y + cta.h == pytest.approx(z.y + z.h - 100, abs=0.5)


def test_solver_size_ratio_enforced():
    spec = _hero_headline_spec()
    result = solve(spec, zones=_safe_zone(), intrinsics=_hero_intrinsics())
    headline_h = result.positions["headline"].h
    subhead_h = result.positions["subhead"].h
    assert headline_h >= subhead_h * 1.5 - 0.5


def test_solver_deterministic():
    """Same inputs (in dict order) → same outputs."""
    spec = _hero_headline_spec()
    zones = _safe_zone()
    intr = _hero_intrinsics()
    runs = [solve(spec, zones=zones, intrinsics=intr).positions for _ in range(5)]
    first = runs[0]
    for r in runs[1:]:
        assert r == first


def test_solver_no_overlap_violation_reported():
    """Force overlap by flipping the bottom anchor and demanding same dims."""
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a", "b"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "zone"},
                {"type": "inside", "element": "b", "zone": "zone"},
                {"type": "anchor", "element": "a", "position": "top_left", "reference": "zone"},
                {"type": "anchor", "element": "b", "position": "top_left", "reference": "zone"},
                {"type": "no_overlap", "elements": ["a", "b"]},
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=500, h=500)}
    intr = {
        "a": ElementIntrinsics(preferred_width=200, preferred_height=200),
        "b": ElementIntrinsics(preferred_width=200, preferred_height=200),
    }
    result = solve(spec, zones=zones, intrinsics=intr)
    assert not result.is_valid
    overlap_conflicts = [c for c in result.conflicts if c.constraint_type == "no_overlap"]
    assert len(overlap_conflicts) == 1
    assert overlap_conflicts[0].phase == "validator"


def test_solver_unknown_reference_returns_prevalidate_conflict():
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "nonexistent"},
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=100, h=100)}
    result = solve(spec, zones=zones, intrinsics={"a": ElementIntrinsics()})
    assert not result.is_valid
    assert any(c.phase == "prevalidate" for c in result.conflicts)


def test_solver_min_size_violation_unsolvable_in_linear_phase():
    """min_size > zone bounds + required-strength inside → unsatisfiable."""
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "zone", "strength": "required"},
                {
                    "type": "min_size",
                    "element": "a",
                    "min_width": 9999,
                    "strength": "required",
                },
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=100, h=100)}
    result = solve(spec, zones=zones)
    assert not result.is_valid
    assert any(c.phase == "linear" for c in result.conflicts)


def test_color_contrast_pass_and_fail():
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["headline", "background"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "headline", "zone": "zone"},
                {"type": "inside", "element": "background", "zone": "zone"},
                {
                    "type": "color_contrast",
                    "foreground": "headline.color",
                    "background": "background.color",
                    "min_ratio": 4.5,
                },
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=200, h=200)}
    intr = {
        "headline": ElementIntrinsics(preferred_width=100, preferred_height=50),
        "background": ElementIntrinsics(preferred_width=200, preferred_height=200),
    }
    # Black on white passes (21:1)
    pass_result = solve(
        spec,
        zones=zones,
        intrinsics=intr,
        content={"headline": {"color": "#000000"}, "background": {"color": "#ffffff"}},
    )
    contrast_conflicts = [c for c in pass_result.conflicts if c.constraint_type == "color_contrast"]
    assert contrast_conflicts == []

    # Light grey on white fails
    fail_result = solve(
        spec,
        zones=zones,
        intrinsics=intr,
        content={"headline": {"color": "#cccccc"}, "background": {"color": "#ffffff"}},
    )
    fail_contrast = [c for c in fail_result.conflicts if c.constraint_type == "color_contrast"]
    assert len(fail_contrast) == 1
    assert "below minimum" in fail_contrast[0].message


def test_wcag_known_values():
    # Black on white = 21:1
    assert wcag_contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    # White on white = 1:1
    assert wcag_contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


def test_max_width_percent_of_zone():
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "zone"},
                {
                    "type": "max_width_percent_of_zone",
                    "element": "a",
                    "zone": "zone",
                    "percent": 50,
                },
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=1000, h=500)}
    result = solve(
        spec,
        zones=zones,
        intrinsics={"a": ElementIntrinsics(preferred_width=900, preferred_height=100)},
    )
    assert result.is_valid
    assert result.positions["a"].w <= 500 + 0.5


def test_horizontal_align_center_to_zone():
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "zone"},
                {
                    "type": "horizontal_align",
                    "element": "a",
                    "align": "center",
                    "reference": "zone",
                },
            ],
        }
    )
    zones = {"zone": Rect(x=100, y=0, w=400, h=200)}
    result = solve(
        spec,
        zones=zones,
        intrinsics={"a": ElementIntrinsics(preferred_width=200, preferred_height=50)},
    )
    a = result.positions["a"]
    # Center of element at center of zone = 300
    assert (a.x + a.w / 2) == pytest.approx(300.0, abs=0.5)


def test_grid_align_violation():
    spec = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "zone"},
                {
                    "type": "anchor",
                    "element": "a",
                    "position": "top_left",
                    "reference": "zone",
                    "margin": 23,  # not on an 8-grid
                },
                {"type": "grid_align", "element": "a", "grid": 8, "axis": "both"},
            ],
        }
    )
    zones = {"zone": Rect(x=0, y=0, w=400, h=400)}
    result = solve(
        spec,
        zones=zones,
        intrinsics={"a": ElementIntrinsics(preferred_width=100, preferred_height=100)},
    )
    grid_conflicts = [c for c in result.conflicts if c.constraint_type == "grid_align"]
    assert len(grid_conflicts) >= 1
