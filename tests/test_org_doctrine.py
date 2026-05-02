"""Unit tests for app.services.org_doctrine — parameter validation.

CRUD paths are tested in test_admin_doctrine_router.py against a fake
DB; here we just exercise the validation function so a typo in the
admin UI gets a clear error before it hits Postgres.
"""

from __future__ import annotations

import pytest

from app.services import org_doctrine


def test_validate_parameters_passes_known_shape():
    out = org_doctrine.validate_parameters(
        {
            "target_margin_pct": 0.40,
            "soft_margin_pct": 0.30,
            "max_capital_outlay_pct_of_revenue": 0.50,
            "min_per_piece_cents": 100,
            "max_per_piece_cents": 800,
            "default_touch_count_by_audience_size_bucket": {
                "0_500": 4,
                "500_2500": 3,
                "2500_10000": 3,
                "10000_plus": 2,
            },
            "model_tier_by_step_type": {"default": "claude-opus-4-7"},
            "gating_mode_default": "auto",
        }
    )
    assert out["target_margin_pct"] == 0.40
    assert out["min_per_piece_cents"] == 100


def test_validate_parameters_coerces_numeric_strings():
    out = org_doctrine.validate_parameters({"target_margin_pct": "0.4"})
    assert out["target_margin_pct"] == 0.4
    out = org_doctrine.validate_parameters({"min_per_piece_cents": "100"})
    assert out["min_per_piece_cents"] == 100


def test_validate_parameters_rejects_non_dict():
    with pytest.raises(org_doctrine.DoctrineValidationError):
        org_doctrine.validate_parameters([1, 2, 3])  # type: ignore[arg-type]


def test_validate_parameters_rejects_non_numeric_for_pct_field():
    with pytest.raises(org_doctrine.DoctrineValidationError) as exc:
        org_doctrine.validate_parameters({"target_margin_pct": "not-a-number"})
    assert "target_margin_pct" in str(exc.value)


def test_validate_parameters_rejects_non_dict_for_object_field():
    with pytest.raises(org_doctrine.DoctrineValidationError) as exc:
        org_doctrine.validate_parameters(
            {"default_touch_count_by_audience_size_bucket": "not-a-dict"}
        )
    assert "default_touch_count_by_audience_size_bucket" in str(exc.value)


def test_validate_parameters_passes_unknown_keys_through():
    out = org_doctrine.validate_parameters(
        {"target_margin_pct": 0.4, "future_key": "future_value"}
    )
    assert out["future_key"] == "future_value"
