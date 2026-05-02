"""DSL Pydantic schema tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.dmaas.dsl import (
    ALL_CONSTRAINT_TYPES,
    LINEAR_CONSTRAINT_TYPES,
    VALIDATOR_CONSTRAINT_TYPES,
    ConstraintSpecification,
)


def test_valid_minimal_spec():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["z"],
            "constraints": [{"type": "inside", "element": "a", "zone": "z"}],
        }
    )
    assert len(s.constraints) == 1


def test_unknown_constraint_type_rejected():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a"],
                "zones": ["z"],
                "constraints": [{"type": "fly_to_moon", "element": "a"}],
            }
        )


def test_size_ratio_validates_accessor_format():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a", "b"],
                "constraints": [
                    {
                        "type": "size_ratio",
                        "larger": "a",  # missing .height/.width
                        "smaller": "b.height",
                        "min_ratio": 1.5,
                    }
                ],
            }
        )


def test_size_ratio_rejects_unknown_dim():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a", "b"],
                "constraints": [
                    {
                        "type": "size_ratio",
                        "larger": "a.banana",
                        "smaller": "b.height",
                        "min_ratio": 1.5,
                    }
                ],
            }
        )


def test_no_overlap_requires_at_least_two_elements():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a"],
                "constraints": [{"type": "no_overlap", "elements": ["a"]}],
            }
        )


def test_strength_validates():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a"],
                "zones": ["z"],
                "constraints": [
                    {"type": "inside", "element": "a", "zone": "z", "strength": "extra-strong"}
                ],
            }
        )


def test_extra_keys_rejected():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a"],
                "zones": ["z"],
                "constraints": [
                    {"type": "inside", "element": "a", "zone": "z", "rogue_key": True}
                ],
            }
        )


def test_validate_references_unknown_element():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["z"],
            "constraints": [{"type": "inside", "element": "ghost", "zone": "z"}],
        }
    )
    errs = s.validate_references()
    assert any("ghost" in e for e in errs)


def test_validate_references_unknown_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["z"],
            "constraints": [{"type": "inside", "element": "a", "zone": "ghost_zone"}],
        }
    )
    errs = s.validate_references()
    assert any("ghost_zone" in e for e in errs)


def test_unique_element_names():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {"elements": ["a", "a"], "zones": [], "constraints": []}
        )


def test_face_default_none_allows_mixed_zones():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a", "b"],
            "zones": ["back_address_block", "front_safe"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "back_address_block"},
                {"type": "inside", "element": "b", "zone": "front_safe"},
            ],
        }
    )
    assert s.face is None
    assert s.validate_references() == []


def test_face_back_accepts_back_and_legacy_zones():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a", "b"],
            "zones": ["back_address_block", "safe_zone"],
            "face": "back",
            "constraints": [
                {"type": "inside", "element": "a", "zone": "back_address_block"},
                {"type": "inside", "element": "b", "zone": "safe_zone"},
            ],
        }
    )
    assert s.validate_references() == []


def test_face_back_rejects_front_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["front_safe"],
            "face": "back",
            "constraints": [
                {"type": "inside", "element": "a", "zone": "front_safe"},
            ],
        }
    )
    errs = s.validate_references()
    assert any("front_safe" in e and "back" in e for e in errs)


def test_face_outside_accepts_panel_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a", "b"],
            "zones": ["outside_top_panel_safe", "outside_address_window"],
            "face": "outside",
            "constraints": [
                {"type": "inside", "element": "a", "zone": "outside_top_panel_safe"},
                {"type": "inside", "element": "b", "zone": "outside_address_window"},
            ],
        }
    )
    assert s.validate_references() == []


def test_face_outside_rejects_inside_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["inside_top_panel_safe"],
            "face": "outside",
            "constraints": [
                {"type": "inside", "element": "a", "zone": "inside_top_panel_safe"},
            ],
        }
    )
    errs = s.validate_references()
    assert any("inside_top_panel_safe" in e and "outside" in e for e in errs)


def test_face_front_accepts_front_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["front_safe"],
            "face": "front",
            "constraints": [{"type": "inside", "element": "a", "zone": "front_safe"}],
        }
    )
    assert s.validate_references() == []


def test_face_inside_accepts_inside_zone():
    s = ConstraintSpecification.model_validate(
        {
            "elements": ["a"],
            "zones": ["inside_top_panel_safe"],
            "face": "inside",
            "constraints": [
                {"type": "inside", "element": "a", "zone": "inside_top_panel_safe"},
            ],
        }
    )
    assert s.validate_references() == []


def test_face_invalid_value_rejected():
    with pytest.raises(ValidationError):
        ConstraintSpecification.model_validate(
            {
                "elements": ["a"],
                "zones": ["front_safe"],
                "face": "sideways",
                "constraints": [{"type": "inside", "element": "a", "zone": "front_safe"}],
            }
        )


def test_seed_scaffold_parses_unchanged():
    import json
    from pathlib import Path

    seed_path = Path(__file__).resolve().parents[1] / "data" / "dmaas_seed_scaffolds.json"
    payload = json.loads(seed_path.read_text())
    spec = payload["scaffolds"][0]["constraint_specification"]
    s = ConstraintSpecification.model_validate(spec)
    assert s.face is None
    assert s.validate_references() == []


def test_constraint_type_taxonomy_complete():
    """The two type sets must be disjoint and union to ALL_CONSTRAINT_TYPES."""
    assert LINEAR_CONSTRAINT_TYPES.isdisjoint(VALIDATOR_CONSTRAINT_TYPES)
    assert LINEAR_CONSTRAINT_TYPES | VALIDATOR_CONSTRAINT_TYPES == ALL_CONSTRAINT_TYPES
