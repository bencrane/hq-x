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


def test_constraint_type_taxonomy_complete():
    """The two type sets must be disjoint and union to ALL_CONSTRAINT_TYPES."""
    assert LINEAR_CONSTRAINT_TYPES.isdisjoint(VALIDATOR_CONSTRAINT_TYPES)
    assert LINEAR_CONSTRAINT_TYPES | VALIDATOR_CONSTRAINT_TYPES == ALL_CONSTRAINT_TYPES
