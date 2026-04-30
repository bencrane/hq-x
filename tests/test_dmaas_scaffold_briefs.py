"""Static checks on the brief library at `data/dmaas_scaffold_briefs/`.

Runs without the agent or the DB — these are file-level invariants:
every brief parses, references a known direct_mail_specs row, declares
a face consistent with its format, and inherits its thesis + acceptance
rules from `data/dmaas_strategies.json` (or overrides them deliberately).

If any of these fail, the brief library has drifted from the substrate
and the seed script will refuse to run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.dmaas.briefs import ScaffoldBrief

ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = ROOT / "data" / "dmaas_scaffold_briefs"
SPECS_PATH = ROOT / "data" / "lob_mailer_specs.json"
STRATEGIES_PATH = ROOT / "data" / "dmaas_strategies.json"
SCAFFOLDS_PATH = ROOT / "data" / "dmaas_v1_scaffolds.json"

EXPECTED_SLUGS = {
    "hero-postcard-front-6x9",
    "proof-postcard-front-6x9",
    "offer-postcard-front-6x9",
    "trust-postcard-front-6x9",
    "hero-self-mailer-outside-11x9-bifold",
    "proof-self-mailer-outside-11x9-bifold",
    "offer-self-mailer-outside-11x9-bifold",
    "trust-self-mailer-outside-11x9-bifold",
}


@pytest.fixture(scope="module")
def specs() -> set[tuple[str, str]]:
    """All (category, variant) pairs available in lob_mailer_specs.json."""
    data = json.loads(SPECS_PATH.read_text())
    return {(s["mailer_category"], s["variant"]) for s in data["specs"]}


@pytest.fixture(scope="module")
def briefs() -> list[ScaffoldBrief]:
    out = []
    for p in sorted(BRIEFS_DIR.glob("*.json")):
        out.append(ScaffoldBrief.model_validate_json(p.read_text()))
    return out


@pytest.fixture(scope="module")
def strategies() -> dict:
    return json.loads(STRATEGIES_PATH.read_text())


def test_v1_brief_library_has_exactly_eight_briefs(briefs):
    assert len(briefs) == 8


def test_v1_brief_library_has_expected_slugs(briefs):
    assert {b.slug for b in briefs} == EXPECTED_SLUGS


def test_each_brief_filename_matches_slug():
    for p in sorted(BRIEFS_DIR.glob("*.json")):
        brief = ScaffoldBrief.model_validate_json(p.read_text())
        assert p.stem == brief.slug, (
            f"filename {p.name} != slug {brief.slug!r}; rename or fix"
        )


def test_each_brief_targets_a_real_spec(briefs, specs):
    for b in briefs:
        for cs in b.compatible_specs:
            assert (cs.category, cs.variant) in specs, (
                f"brief {b.slug!r} targets unknown spec {(cs.category, cs.variant)}"
            )


def test_face_is_consistent_with_format(briefs):
    """Postcards design on `front` (back is platform-templated address side).
    Self-mailers design on `outside` (inside is out of v1 scope)."""
    for b in briefs:
        if b.format == "postcard":
            assert b.face == "front", f"{b.slug}: postcard face must be 'front'"
        elif b.format == "self_mailer":
            assert b.face == "outside", f"{b.slug}: self_mailer face must be 'outside'"


def test_strategy_inherits_default_required_slots(briefs, strategies):
    """Each brief MAY override required_slots, but the v1 library's briefs
    all carry the strategy's default required_slots — drift here means
    something has changed and the system prompt should be re-checked."""
    for b in briefs:
        defaults = strategies[b.strategy]["default_required_slots"]
        assert set(defaults).issubset(set(b.required_slots)), (
            f"{b.slug}: brief required_slots {b.required_slots} does not include "
            f"{b.strategy} defaults {defaults}"
        )


def test_brief_thesis_is_non_empty_and_strategy_term_appears(briefs, strategies):
    """Briefs may override the strategy's normative thesis prose (the
    self-mailer briefs do, to add panel-specific guidance), but every
    brief must (a) have a non-empty thesis, and (b) reference the strategy
    by name or by a distinctive keyword from the strategy's normative
    thesis. Strategy-distinctive keywords: hero='dominant'/'lead',
    proof='Trust'/'credibility'/'proof', offer='offer'/'discount'/'rebate',
    trust='Authority'/'tenure'/'years in business'."""
    keywords = {
        "hero": ("dominant", "lead", "BIG NEWS"),
        "proof": ("Trust", "credibility", "proof", "credentials"),
        "offer": ("offer", "discount", "rebate", "fixed-price"),
        "trust": ("Authority", "tenure", "years in business", "Established"),
    }
    for b in briefs:
        assert len(b.thesis) > 80, f"{b.slug}: thesis is suspiciously short"
        words = keywords[b.strategy]
        assert any(w in b.thesis for w in words), (
            f"{b.slug}: thesis lacks any {b.strategy} keyword from {words!r}; "
            f"strategy intent may be drifting."
        )


def test_brief_acceptance_rules_match_or_extend_strategy_defaults(briefs, strategies):
    """Briefs inherit the strategy's default_acceptance_rules. They may add
    rules but may not silently drop them — that would change what the
    strategy means in v1."""
    for b in briefs:
        defaults = strategies[b.strategy]["default_acceptance_rules"]
        brief_rule_summaries = {
            (r.type, getattr(r, "element", None) or getattr(r, "slot", None) or getattr(r, "category", None))
            for r in b.acceptance_rules
        }
        for d in defaults:
            key = (d["type"], d.get("element") or d.get("slot") or d.get("category") or d.get("larger"))
            # For size_hierarchy the discriminator key includes 'larger' since
            # there's no element / slot / category.
            if d["type"] == "size_hierarchy":
                key = ("size_hierarchy", d["larger"])
                brief_rule_summaries = {
                    (r.type, getattr(r, "larger", None) if r.type == "size_hierarchy" else None)
                    if r.type == "size_hierarchy" else
                    (r.type, getattr(r, "element", None) or getattr(r, "slot", None) or getattr(r, "category", None))
                    for r in b.acceptance_rules
                }
            assert key in brief_rule_summaries, (
                f"{b.slug}: missing default rule {d!r} from strategy {b.strategy}"
            )


def test_each_brief_required_slot_is_in_placeholder_content(briefs):
    """Placeholder content must cover every required slot — without it the
    seed script can't run a full solve at create_scaffold time."""
    for b in briefs:
        for slot in b.required_slots:
            assert slot in b.placeholder_content, (
                f"{b.slug}: required slot {slot!r} missing from placeholder_content"
            )


# ---------------------------------------------------------------------------
# Brief ↔ scaffold linkage
# ---------------------------------------------------------------------------


def test_every_brief_has_a_matching_scaffold():
    """data/dmaas_v1_scaffolds.json must contain one entry per brief slug."""
    scaffolds = json.loads(SCAFFOLDS_PATH.read_text())["scaffolds"]
    scaffold_slugs = {s["slug"] for s in scaffolds}
    brief_slugs = {p.stem for p in BRIEFS_DIR.glob("*.json")}
    assert scaffold_slugs == brief_slugs, (
        f"brief/scaffold drift: only-briefs={brief_slugs - scaffold_slugs}, "
        f"only-scaffolds={scaffold_slugs - brief_slugs}"
    )


def test_scaffold_strategy_matches_brief_strategy():
    scaffolds = {s["slug"]: s for s in json.loads(SCAFFOLDS_PATH.read_text())["scaffolds"]}
    for p in BRIEFS_DIR.glob("*.json"):
        brief = ScaffoldBrief.model_validate_json(p.read_text())
        scaffold = scaffolds[brief.slug]
        assert scaffold["strategy"] == brief.strategy
        assert scaffold["format"] == brief.format
        assert scaffold["constraint_specification"]["face"] == brief.face
