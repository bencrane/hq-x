"""Direct-mail spec endpoint tests.

Auth is bypassed via FastAPI dependency_overrides (see test_direct_mail_endpoints).
DB calls (`list_specs`, `get_spec`, `list_categories`, `list_design_rules`)
are monkeypatched to canned in-memory data so tests don't need Supabase.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.direct_mail import specs as specs_module
from app.direct_mail.specs import MailerSpec
from app.main import app
from app.routers import direct_mail as direct_mail_router

_TEST_USER = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    email="op@example.com",
    platform_role="platform_operator",
    active_organization_id=None,
    org_role=None,
    role="operator",
    client_id=None,
)


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _TEST_USER
    app.dependency_overrides[require_operator] = lambda: _TEST_USER
    yield
    app.dependency_overrides.clear()


def _postcard_4x6() -> MailerSpec:
    return MailerSpec(
        id="00000000-0000-0000-0000-000000000001",
        mailer_category="postcard",
        variant="4x6",
        label='Postcard 4x6"',
        bleed_w_in=4.25,
        bleed_h_in=6.25,
        trim_w_in=4.0,
        trim_h_in=6.0,
        safe_inset_in=0.125,
        zones={
            "ink_free": {
                "panel": "back",
                "w_in": 3.2835,
                "h_in": 2.375,
                "from_right_in": 0.275,
                "from_bottom_in": 0.25,
            },
            "usps_scan_warning": {
                "panel": "front",
                "rule": "Avoid address-like content in bottom 2.375\"",
                "h_in": 2.375,
                "anchor": "bottom",
            },
        },
        folding=None,
        pagination=None,
        address_placement=None,
        envelope=None,
        production={"required_dpi": 300, "full_bleed": True},
        ordering={},
        template_pdf_url="https://example.com/4x6.pdf",
        additional_template_urls=[],
        source_urls=["https://help.lob.com/postcards"],
        notes=None,
    )


def _letter_8x11() -> MailerSpec:
    return MailerSpec(
        id="00000000-0000-0000-0000-000000000002",
        mailer_category="letter",
        variant="8.5x11_standard",
        label='Letter 8.5x11" (Standard)',
        bleed_w_in=None,
        bleed_h_in=None,
        trim_w_in=8.5,
        trim_h_in=11.0,
        safe_inset_in=0.0625,
        zones={"clear_space": {"all_sides_in": 0.0625}},
        folding=None,
        pagination=None,
        address_placement={"modes": ["top_first_page", "insert_blank_page"]},
        envelope={"1_to_6_sheets": "#10 double-window, C-fold"},
        production={"required_dpi": 300, "full_bleed": False},
        ordering={"max_sheets": 60},
        template_pdf_url="https://example.com/letter.pdf",
        additional_template_urls=[],
        source_urls=["https://help.lob.com/letters"],
        notes=None,
    )


@pytest.fixture
def stub_specs_db(monkeypatch):
    """In-memory replacement for the DB query helpers."""
    rows = [_postcard_4x6(), _letter_8x11()]

    async def fake_list_specs(category=None):
        # Mirror the real query's ORDER BY (mailer_category, variant)
        out = [r for r in rows if category is None or r.mailer_category == category]
        return sorted(out, key=lambda r: (r.mailer_category, r.variant))

    async def fake_get_spec(category, variant):
        for r in rows:
            if r.mailer_category == category and r.variant == variant:
                return r
        return None

    async def fake_list_categories():
        from collections import defaultdict

        d: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            d[r.mailer_category].append(r.variant)
        return [
            {"category": k, "variant_count": len(v), "variants": sorted(v)}
            for k, v in sorted(d.items())
        ]

    async def fake_list_design_rules():
        return [
            {
                "key": "required_dpi_default",
                "value": 300,
                "description": None,
                "source_url": "https://help.lob.com/artboard",
                "updated_at": "2026-04-28T00:00:00+00:00",
            },
            {
                "key": "safe_zone_universal_in",
                "value": 0.125,
                "description": None,
                "source_url": "https://help.lob.com/artboard",
                "updated_at": "2026-04-28T00:00:00+00:00",
            },
        ]

    monkeypatch.setattr(direct_mail_router, "list_specs", fake_list_specs)
    monkeypatch.setattr(direct_mail_router, "get_spec", fake_get_spec)
    monkeypatch.setattr(direct_mail_router, "list_categories", fake_list_categories)
    monkeypatch.setattr(direct_mail_router, "list_design_rules", fake_list_design_rules)
    return rows


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_list_specs_returns_full_objects(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # Order is (category, variant)
    assert body["specs"][0]["mailer_category"] == "letter"
    assert body["specs"][1]["mailer_category"] == "postcard"
    # Spec object includes zones / production / source_urls
    pc = body["specs"][1]
    assert pc["bleed_w_in"] == 4.25
    assert pc["zones"]["ink_free"]["w_in"] == 3.2835
    assert "https://help.lob.com/postcards" in pc["source_urls"]


@pytest.mark.asyncio
async def test_list_specs_category_filter(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs?category=postcard")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["specs"][0]["variant"] == "4x6"


@pytest.mark.asyncio
async def test_list_specs_invalid_category_400(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs?category=banana")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_category"


@pytest.mark.asyncio
async def test_categories_route(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs/categories")
    assert r.status_code == 200
    body = r.json()
    cats = {c["category"]: c for c in body["categories"]}
    assert cats["postcard"]["variant_count"] == 1
    assert cats["postcard"]["variants"] == ["4x6"]


@pytest.mark.asyncio
async def test_design_rules_route(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs/design-rules")
    assert r.status_code == 200
    rules = {row["key"]: row["value"] for row in r.json()["rules"]}
    assert rules["required_dpi_default"] == 300
    assert rules["safe_zone_universal_in"] == 0.125


@pytest.mark.asyncio
async def test_get_one_spec(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs/postcard/4x6")
    assert r.status_code == 200
    body = r.json()
    assert body["variant"] == "4x6"
    assert body["zones"]["ink_free"]["from_right_in"] == 0.275


@pytest.mark.asyncio
async def test_get_one_spec_404(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs/postcard/99x99")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "spec_not_found"


@pytest.mark.asyncio
async def test_get_one_spec_invalid_category_400(stub_specs_db):
    async with await _client() as c:
        r = await c.get("/direct-mail/specs/banana/foo")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_validate_passes_for_correct_dimensions(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 4.25, "height_in": 6.25, "dpi": 300},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["is_valid"] is True
    assert body["error_count"] == 0
    # Validate response embeds the full spec — frontend doesn't need a 2nd round-trip.
    assert body["spec"]["mailer_category"] == "postcard"
    assert body["spec"]["zones"]["ink_free"]["w_in"] == 3.2835


@pytest.mark.asyncio
async def test_validate_passes_with_either_orientation(stub_specs_db):
    """Lob ships templates in both portrait and landscape; either should pass."""
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 6.25, "height_in": 4.25, "dpi": 300},
        )
    assert r.status_code == 200
    assert r.json()["is_valid"] is True


@pytest.mark.asyncio
async def test_validate_dimension_mismatch_is_error(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 5.0, "height_in": 7.0, "dpi": 300},
        )
    body = r.json()
    assert body["is_valid"] is False
    assert body["error_count"] >= 1
    codes = [c["code"] for c in body["checks"]]
    assert "dimensions_mismatch" in codes


@pytest.mark.asyncio
async def test_validate_low_dpi_warning(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 4.25, "height_in": 6.25, "dpi": 250},
        )
    body = r.json()
    assert body["is_valid"] is True  # warnings don't fail
    assert body["warning_count"] == 1
    assert any(c["code"] == "dpi_low" for c in body["checks"])


@pytest.mark.asyncio
async def test_validate_too_low_dpi_is_error(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 4.25, "height_in": 6.25, "dpi": 150},
        )
    body = r.json()
    assert body["is_valid"] is False
    codes = [c["code"] for c in body["checks"]]
    assert "dpi_too_low" in codes


@pytest.mark.asyncio
async def test_validate_letter_uses_trim_when_no_bleed(stub_specs_db):
    """Letters have no bleed — validator should match against trim."""
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/letter/8.5x11_standard/validate",
            json={"width_in": 8.5, "height_in": 11.0, "dpi": 300},
        )
    body = r.json()
    assert body["is_valid"] is True
    dim_check = next(c for c in body["checks"] if c["code"] == "dimensions_match")
    assert dim_check["expected"]["anchor"] == "trim"


@pytest.mark.asyncio
async def test_validate_panel_filters_zones(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/4x6/validate",
            json={"width_in": 4.25, "height_in": 6.25, "panel": "back"},
        )
    body = r.json()
    panel_check = next(c for c in body["checks"] if c["code"] == "zones_for_panel")
    zone_names = [z["zone"] for z in panel_check["actual"]["zones"]]
    assert "ink_free" in zone_names
    # USPS warning is for front, not back — should be excluded.
    assert "usps_scan_warning" not in zone_names


@pytest.mark.asyncio
async def test_validate_404_when_spec_missing(stub_specs_db):
    async with await _client() as c:
        r = await c.post(
            "/direct-mail/specs/postcard/99x99/validate",
            json={"width_in": 4.0, "height_in": 6.0},
        )
    assert r.status_code == 404
