"""Spec → binding tests for the v1 zone catalog.

Builds MailerSpec objects directly from `data/lob_mailer_specs.json` (no DB)
and runs them through `bind_spec_zones` to assert every postcard / self-mailer
bifold produces the named zones the DMaaS DSL needs.

These are the regression guard for the next directive (DMaaS scaffold-authoring
managed agent): if the agent issues a constraint referencing `back_address_block`
or `outside_top_panel_safe`, the binding had better produce that zone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.direct_mail.specs import MailerSpec
from app.dmaas.service import bind_spec_zones, binding_to_dict


SPEC_JSON = Path(__file__).resolve().parent.parent / "data" / "lob_mailer_specs.json"


def _spec_from_row(s: dict) -> MailerSpec:
    return MailerSpec(
        id=s.get("id", "json"),
        mailer_category=s["mailer_category"],
        variant=s["variant"],
        label=s["label"],
        bleed_w_in=s.get("bleed_w_in"),
        bleed_h_in=s.get("bleed_h_in"),
        trim_w_in=s["trim_w_in"],
        trim_h_in=s["trim_h_in"],
        safe_inset_in=s.get("safe_inset_in"),
        zones=s.get("zones") or {},
        folding=s.get("folding"),
        pagination=s.get("pagination"),
        address_placement=s.get("address_placement"),
        envelope=s.get("envelope"),
        production=s.get("production") or {},
        ordering=s.get("ordering") or {},
        template_pdf_url=s.get("template_pdf_url"),
        additional_template_urls=s.get("additional_template_urls") or [],
        source_urls=s.get("source_urls") or [],
        notes=s.get("notes"),
        faces=s.get("faces") or [],
    )


def _all_specs() -> dict[tuple[str, str], dict]:
    rows = json.loads(SPEC_JSON.read_text())["specs"]
    return {(r["mailer_category"], r["variant"]): r for r in rows}


SPECS = _all_specs()


POSTCARD_VARIANTS = ["4x6", "5x7", "6x9", "6x11"]
SELF_MAILER_BIFOLD_VARIANTS = ["11x9_bifold", "12x9_bifold", "6x18_bifold"]

POSTCARD_REQUIRED_ZONES = {
    "front_face", "back_face",
    "front_safe", "back_safe",
    "back_address_block", "back_postage_indicia",
    "back_return_address", "back_usps_barcode_clear",
    "back_ink_free",
    "front_usps_scan_warning",
}

SELF_MAILER_REQUIRED_ZONES = {
    "outside_face", "inside_face",
    "outside_address_window", "outside_postage_indicia",
    "outside_usps_barcode_clear", "outside_ink_free",
    "fold_gutter_1",
}


# ---------------------------------------------------------------------------
# Per-format zone presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", POSTCARD_VARIANTS)
def test_postcard_required_zones_present(variant):
    spec = _spec_from_row(SPECS[("postcard", variant)])
    binding = bind_spec_zones(spec)
    missing = POSTCARD_REQUIRED_ZONES - set(binding.zones)
    assert not missing, f"postcard/{variant}: missing {missing}"


@pytest.mark.parametrize("variant", SELF_MAILER_BIFOLD_VARIANTS)
def test_self_mailer_required_zones_present(variant):
    spec = _spec_from_row(SPECS[("self_mailer", variant)])
    binding = bind_spec_zones(spec)
    missing = SELF_MAILER_REQUIRED_ZONES - set(binding.zones)
    assert not missing, f"self_mailer/{variant}: missing {missing}"
    # Cover panel + cover-panel-safe are also required and depend on naming.
    cover = (spec.folding or {}).get("cover_panel")
    assert cover in binding.zones, f"missing cover panel zone {cover}"
    assert f"{cover}_safe" in binding.zones, f"missing {cover}_safe"


# ---------------------------------------------------------------------------
# Concrete coordinate locks (regression guards)
# ---------------------------------------------------------------------------


def test_postcard_6x9_back_address_block_pixel_coordinates():
    """6x9 @ 300 DPI: address_block w=3.5, h=1.5, from_right=0.525, from_bottom=0.875.
    bleed face_w=6.25, face_h=9.25 → 1875 × 2775 px. Lock the coordinates."""
    spec = _spec_from_row(SPECS[("postcard", "6x9")])
    binding = bind_spec_zones(spec)
    z = binding.zones["back_address_block"]
    assert z.w == pytest.approx(1050, abs=0.5)  # 3.5 * 300
    assert z.h == pytest.approx(450, abs=0.5)   # 1.5 * 300
    # x = (face_w - w - from_right) * dpi = (6.25 - 3.5 - 0.525) * 300 = 2.225 * 300
    assert z.x == pytest.approx(667.5, abs=0.5)
    # y = (face_h - h - from_bottom) * dpi = (9.25 - 1.5 - 0.875) * 300 = 6.875 * 300
    assert z.y == pytest.approx(2062.5, abs=0.5)


def test_self_mailer_11x9_outside_top_panel_pixel_coordinates():
    """11x9_bifold horizontal fold at y=5: top panel is 11"w × 5"h in trim,
    bleed margins 0.125" each side → in canvas coords (300 dpi):
        x = bleed_margin_x = 0.125 * 300 = 37.5
        y = bleed_margin_y = 0.125 * 300 = 37.5
        w = trim_w * 300 = 11 * 300 = 3300
        h = 5 * 300 = 1500"""
    spec = _spec_from_row(SPECS[("self_mailer", "11x9_bifold")])
    binding = bind_spec_zones(spec)
    p = binding.zones["outside_top_panel"]
    assert p.x == pytest.approx(37.5, abs=0.5)
    assert p.y == pytest.approx(37.5, abs=0.5)
    assert p.w == pytest.approx(3300, abs=0.5)
    assert p.h == pytest.approx(1500, abs=0.5)


# ---------------------------------------------------------------------------
# Non-overlap invariants (mirror sync_lob_specs.py checks)
# ---------------------------------------------------------------------------


def _overlap(a, b, eps=0.5) -> bool:
    return not (
        a.x + a.w <= b.x + eps
        or b.x + b.w <= a.x + eps
        or a.y + a.h <= b.y + eps
        or b.y + b.h <= a.y + eps
    )


@pytest.mark.parametrize("variant", POSTCARD_VARIANTS)
def test_postcard_back_face_zones_non_overlapping(variant):
    spec = _spec_from_row(SPECS[("postcard", variant)])
    z = bind_spec_zones(spec).zones
    names = ["back_address_block", "back_postage_indicia", "back_return_address", "back_usps_barcode_clear"]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            assert not _overlap(z[names[i]], z[names[j]]), (
                f"postcard/{variant}: {names[i]} overlaps {names[j]}"
            )


@pytest.mark.parametrize("variant", SELF_MAILER_BIFOLD_VARIANTS)
def test_self_mailer_cover_panel_zones_non_overlapping(variant):
    spec = _spec_from_row(SPECS[("self_mailer", variant)])
    z = bind_spec_zones(spec).zones
    names = ["outside_address_window", "outside_postage_indicia", "outside_usps_barcode_clear"]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            assert not _overlap(z[names[i]], z[names[j]]), (
                f"self_mailer/{variant}: {names[i]} overlaps {names[j]}"
            )


@pytest.mark.parametrize("variant", POSTCARD_VARIANTS)
def test_postcard_safe_inside_face(variant):
    z = bind_spec_zones(_spec_from_row(SPECS[("postcard", variant)])).zones
    for face in ("front", "back"):
        face_rect, safe_rect = z[f"{face}_face"], z[f"{face}_safe"]
        assert safe_rect.x >= face_rect.x - 0.5
        assert safe_rect.y >= face_rect.y - 0.5
        assert safe_rect.x + safe_rect.w <= face_rect.x + face_rect.w + 0.5
        assert safe_rect.y + safe_rect.h <= face_rect.y + face_rect.h + 0.5


@pytest.mark.parametrize("variant", SELF_MAILER_BIFOLD_VARIANTS)
def test_self_mailer_panel_safe_inside_panel(variant):
    spec = _spec_from_row(SPECS[("self_mailer", variant)])
    z = bind_spec_zones(spec).zones
    cover = (spec.folding or {}).get("cover_panel")
    panel_rect, safe_rect = z[cover], z[f"{cover}_safe"]
    assert safe_rect.x >= panel_rect.x - 0.5
    assert safe_rect.y >= panel_rect.y - 0.5
    assert safe_rect.x + safe_rect.w <= panel_rect.x + panel_rect.w + 0.5
    assert safe_rect.y + safe_rect.h <= panel_rect.y + panel_rect.h + 0.5


# ---------------------------------------------------------------------------
# Region typing
# ---------------------------------------------------------------------------


def test_regions_carry_face_and_source_metadata():
    spec = _spec_from_row(SPECS[("postcard", "6x9")])
    binding = bind_spec_zones(spec)
    by_name = {r.name: r for r in binding.regions}
    assert by_name["back_address_block"].face == "back"
    assert by_name["back_address_block"].type == "address_block"
    assert by_name["back_address_block"].source == "usps_dmm"
    assert by_name["back_postage_indicia"].source == "usps_dmm"
    assert by_name["back_ink_free"].source == "lob_help_center"
    assert by_name["front_usps_scan_warning"].face == "front"


def test_self_mailer_regions_have_panel_metadata():
    spec = _spec_from_row(SPECS[("self_mailer", "11x9_bifold")])
    binding = bind_spec_zones(spec)
    by_name = {r.name: r for r in binding.regions}
    aw = by_name["outside_address_window"]
    assert aw.face == "outside"
    assert aw.panel == "outside_top_panel"
    assert aw.type == "address_window"
    # Glue zones have face but no panel
    glue = by_name["glue_zone_top"]
    assert glue.type == "glue"
    assert glue.face == "outside"


# ---------------------------------------------------------------------------
# Trifold + future-format compatibility (model accommodates without code change)
# ---------------------------------------------------------------------------


def test_trifold_panels_derived_even_without_faces_data():
    """The trifold has no `faces` populated yet (out of v1 scope) but the
    panel-derivation logic must still produce 3 outside + 3 inside panels
    from the fold-line metadata. Demonstrates the model is additive."""
    spec = _spec_from_row(SPECS[("self_mailer", "17.75x9_trifold")])
    binding = bind_spec_zones(spec)
    panel_names = [r.name for r in binding.regions if r.type == "panel"]
    assert "outside_left_panel" in panel_names
    assert "outside_middle_panel" in panel_names
    assert "outside_right_panel" in panel_names
    assert "inside_left_panel" in panel_names
    assert "inside_middle_panel" in panel_names
    assert "inside_right_panel" in panel_names
    # Glue + fold gutters still derive from folding metadata
    region_names = {r.name for r in binding.regions}
    assert "fold_gutter_1" in region_names
    assert "fold_gutter_2" in region_names


# ---------------------------------------------------------------------------
# Compatible_specs round-trip — every entry in dmaas_seed_scaffolds.json's
# compatible_specs must bind without exception and expose all expected v1
# zones.
# ---------------------------------------------------------------------------


def test_seed_scaffold_compatible_specs_all_resolve():
    seed_path = Path(__file__).resolve().parent.parent / "data" / "dmaas_seed_scaffolds.json"
    seed = json.loads(seed_path.read_text())
    for s in seed["scaffolds"]:
        for cs in s["compatible_specs"]:
            row = SPECS[(cs["category"], cs["variant"])]
            spec = _spec_from_row(row)
            binding = bind_spec_zones(spec)
            assert "safe_zone" in binding.zones, (
                f"compatible spec {cs} missing legacy safe_zone — "
                "would break the seeded hero-headline-postcard scaffold"
            )


# ---------------------------------------------------------------------------
# binding_to_dict marshalling
# ---------------------------------------------------------------------------


def test_binding_to_dict_serializable():
    spec = _spec_from_row(SPECS[("postcard", "6x9")])
    d = binding_to_dict(bind_spec_zones(spec))
    # Every region has the typed metadata MCP consumers expect
    region_types = {r["type"] for r in d["regions"]}
    assert "address_block" in region_types
    assert "barcode_clear" in region_types
    assert "postage" in region_types
    assert "informational" in region_types  # usps_scan_warning
    assert d["faces"][0]["name"] in ("front", "back")
    # JSON-serializable
    json.dumps(d)
