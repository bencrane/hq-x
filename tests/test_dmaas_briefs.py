"""Unit tests for `app.dmaas.briefs`.

Covers ScaffoldBrief parsing, AcceptanceRule discriminated-union dispatch,
and each rule variant's evaluator against synthetic resolved positions."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.dmaas.briefs import (
    AcceptanceFailure,
    AreaDominanceRule,
    MinSlotCountRule,
    ScaffoldBrief,
    SizeHierarchyRule,
    SlotPresentRule,
    evaluate_rules,
)


# ---------------------------------------------------------------------------
# ScaffoldBrief parsing
# ---------------------------------------------------------------------------


def _minimal_brief() -> dict:
    return {
        "slug": "hero-postcard-front-6x9",
        "name": "Hero Postcard",
        "strategy": "hero",
        "face": "front",
        "format": "postcard",
        "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
        "thesis": "thesis text",
        "required_slots": ["headline", "cta"],
        "optional_slots": ["subhead"],
        "acceptance_rules": [],
        "placeholder_content": {},
    }


def test_brief_parses_minimal():
    b = ScaffoldBrief.model_validate(_minimal_brief())
    assert b.slug == "hero-postcard-front-6x9"
    assert b.strategy == "hero"
    assert b.face == "front"
    assert b.format == "postcard"
    assert b.compatible_specs[0].category == "postcard"


def test_brief_rejects_unknown_strategy():
    bad = _minimal_brief()
    bad["strategy"] = "weird"
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_brief_rejects_unknown_face():
    bad = _minimal_brief()
    bad["face"] = "diagonal"
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_brief_rejects_unknown_format():
    bad = _minimal_brief()
    bad["format"] = "letter"
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_brief_rejects_extra_fields():
    bad = _minimal_brief()
    bad["something_else"] = 1
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_brief_requires_at_least_one_compatible_spec():
    bad = _minimal_brief()
    bad["compatible_specs"] = []
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_brief_slug_pattern():
    bad = _minimal_brief()
    bad["slug"] = "Hero Postcard"
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


# ---------------------------------------------------------------------------
# AcceptanceRule discriminated union
# ---------------------------------------------------------------------------


def test_acceptance_rule_discriminator():
    """All four variants parse via the brief's `acceptance_rules` field."""
    body = _minimal_brief()
    body["acceptance_rules"] = [
        {"type": "size_hierarchy", "larger": "headline", "smaller": "subhead", "min_ratio": 1.5},
        {"type": "area_dominance", "element": "offer_block", "min_ratio_vs_others": 1.5},
        {"type": "slot_present", "slot": "credential_strip"},
        {"type": "min_slot_count", "category": "proof", "min": 2},
    ]
    b = ScaffoldBrief.model_validate(body)
    assert isinstance(b.acceptance_rules[0], SizeHierarchyRule)
    assert isinstance(b.acceptance_rules[1], AreaDominanceRule)
    assert isinstance(b.acceptance_rules[2], SlotPresentRule)
    assert isinstance(b.acceptance_rules[3], MinSlotCountRule)


def test_acceptance_rule_rejects_invalid_ratio():
    bad = _minimal_brief()
    bad["acceptance_rules"] = [
        {"type": "size_hierarchy", "larger": "h", "smaller": "s", "min_ratio": 0}
    ]
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


def test_acceptance_rule_rejects_unknown_type():
    bad = _minimal_brief()
    bad["acceptance_rules"] = [{"type": "magic", "element": "x"}]
    with pytest.raises(ValidationError):
        ScaffoldBrief.model_validate(bad)


# ---------------------------------------------------------------------------
# evaluate_rules — each variant
# ---------------------------------------------------------------------------


def test_size_hierarchy_passes():
    rule = SizeHierarchyRule(type="size_hierarchy", larger="headline", smaller="subhead", min_ratio=1.5)
    positions = {
        "headline": {"x": 0, "y": 0, "w": 100, "h": 200},
        "subhead": {"x": 0, "y": 0, "w": 100, "h": 100},
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert failures == []


def test_size_hierarchy_fails_when_too_close():
    rule = SizeHierarchyRule(type="size_hierarchy", larger="headline", smaller="subhead", min_ratio=2.0)
    positions = {
        "headline": {"x": 0, "y": 0, "w": 100, "h": 200},
        "subhead": {"x": 0, "y": 0, "w": 100, "h": 110},
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert len(failures) == 1
    assert failures[0].rule_type == "size_hierarchy"
    assert "1.82×" in failures[0].message


def test_size_hierarchy_handles_missing_element():
    rule = SizeHierarchyRule(type="size_hierarchy", larger="headline", smaller="missing", min_ratio=1.5)
    positions = {"headline": {"x": 0, "y": 0, "w": 100, "h": 200}}
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert len(failures) == 1
    assert "missing" in failures[0].message


def test_size_hierarchy_handles_zero_height_smaller():
    rule = SizeHierarchyRule(type="size_hierarchy", larger="headline", smaller="subhead", min_ratio=1.5)
    positions = {
        "headline": {"x": 0, "y": 0, "w": 100, "h": 200},
        "subhead": {"x": 0, "y": 0, "w": 100, "h": 0},
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert len(failures) == 1
    assert "zero height" in failures[0].message


def test_area_dominance_passes():
    rule = AreaDominanceRule(type="area_dominance", element="offer", min_ratio_vs_others=1.5)
    positions = {
        "offer": {"x": 0, "y": 0, "w": 1000, "h": 800},
        "headline": {"x": 0, "y": 0, "w": 100, "h": 100},
        "cta": {"x": 0, "y": 0, "w": 200, "h": 100},
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert failures == []


def test_area_dominance_fails_against_one_competitor():
    rule = AreaDominanceRule(type="area_dominance", element="offer", min_ratio_vs_others=10.0)
    positions = {
        "offer": {"x": 0, "y": 0, "w": 1000, "h": 800},  # 800,000
        "headline": {"x": 0, "y": 0, "w": 1000, "h": 600},  # 600,000 — only 1.33× smaller
        "cta": {"x": 0, "y": 0, "w": 100, "h": 100},
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    # One failure per competitor that violates the ratio.
    assert any("headline" in f.message for f in failures)
    assert all(f.rule_type == "area_dominance" for f in failures)


def test_area_dominance_handles_zero_area_competitor():
    rule = AreaDominanceRule(type="area_dominance", element="offer", min_ratio_vs_others=2.0)
    positions = {
        "offer": {"x": 0, "y": 0, "w": 100, "h": 100},
        "ghost": {"x": 0, "y": 0, "w": 0, "h": 0},  # zero-area: skipped
    }
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert failures == []


def test_area_dominance_handles_missing_target():
    rule = AreaDominanceRule(type="area_dominance", element="offer", min_ratio_vs_others=2.0)
    positions = {"headline": {"x": 0, "y": 0, "w": 100, "h": 100}}
    failures = evaluate_rules([rule], positions=positions, prop_schema={})
    assert len(failures) == 1
    assert "not in resolved positions" in failures[0].message


def test_slot_present_passes():
    rule = SlotPresentRule(type="slot_present", slot="proof_strip")
    schema = {"properties": {"proof_strip": {}, "headline": {}}}
    failures = evaluate_rules([rule], positions={}, prop_schema=schema)
    assert failures == []


def test_slot_present_fails_when_slot_missing():
    rule = SlotPresentRule(type="slot_present", slot="proof_strip")
    schema = {"properties": {"headline": {}}}
    failures = evaluate_rules([rule], positions={}, prop_schema=schema)
    assert len(failures) == 1
    assert "proof_strip" in failures[0].message


def test_min_slot_count_passes():
    rule = MinSlotCountRule(type="min_slot_count", category="proof", min=2)
    schema = {"properties": {"proof_logo_1": {}, "proof_logo_2": {}, "headline": {}}}
    failures = evaluate_rules([rule], positions={}, prop_schema=schema)
    assert failures == []


def test_min_slot_count_fails_below_threshold():
    rule = MinSlotCountRule(type="min_slot_count", category="proof", min=3)
    schema = {"properties": {"proof_logo_1": {}, "proof_logo_2": {}, "headline": {}}}
    failures = evaluate_rules([rule], positions={}, prop_schema=schema)
    assert len(failures) == 1
    assert failures[0].detail["found"] == 2
    assert failures[0].detail["required"] == 3


def test_evaluate_rules_aggregates_multiple_failures():
    rules = [
        SizeHierarchyRule(type="size_hierarchy", larger="h", smaller="s", min_ratio=2.0),
        SlotPresentRule(type="slot_present", slot="proof_strip"),
    ]
    positions = {
        "h": {"x": 0, "y": 0, "w": 100, "h": 100},
        "s": {"x": 0, "y": 0, "w": 100, "h": 100},
    }
    schema = {"properties": {"h": {}, "s": {}}}
    failures = evaluate_rules(rules, positions=positions, prop_schema=schema)
    assert len(failures) == 2
    assert {f.rule_type for f in failures} == {"size_hierarchy", "slot_present"}


def test_acceptance_failure_is_serializable():
    """Used by the seed script to log structured failure to authoring sessions."""
    rule = SlotPresentRule(type="slot_present", slot="x")
    failures = evaluate_rules([rule], positions={}, prop_schema={"properties": {}})
    assert len(failures) == 1
    payload = failures[0].model_dump()
    assert payload["rule_type"] == "slot_present"
    assert payload["detail"] == {"slot": "x"}
    # round-trip
    AcceptanceFailure.model_validate(json.loads(failures[0].model_dump_json()))
