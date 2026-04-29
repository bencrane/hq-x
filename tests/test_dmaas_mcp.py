"""MCP tool dispatch tests.

FastMCP tools are async callables underneath the @mcp.tool decorator. We
exercise them in-process — same call path the MCP transport will hit, but
without the HTTP/SSE overhead. Verifies parameter validation, tool dispatch,
and that returns are MCP-serializable plain dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.direct_mail.specs import MailerSpec
from app.dmaas import repository as repo
from app.dmaas.repository import Design, Scaffold
from app.mcp import dmaas as mcp_module


def _postcard_6x9() -> MailerSpec:
    return MailerSpec(
        id="00000000-0000-0000-0000-000000000099",
        mailer_category="postcard",
        variant="6x9",
        label='Postcard 6x9"',
        bleed_w_in=6.25,
        bleed_h_in=9.25,
        trim_w_in=6.0,
        trim_h_in=9.0,
        safe_inset_in=0.125,
        zones={},
        folding=None,
        pagination=None,
        address_placement=None,
        envelope=None,
        production={"required_dpi": 300, "full_bleed": True},
        ordering={},
        template_pdf_url=None,
        additional_template_urls=[],
        source_urls=[],
        notes=None,
    )


def _hero_spec() -> dict:
    return {
        "elements": ["headline", "cta"],
        "zones": ["safe_zone"],
        "constraints": [
            {"type": "inside", "element": "headline", "zone": "safe_zone"},
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
            {"type": "no_overlap", "elements": ["headline", "cta"]},
        ],
    }


@pytest.fixture
def mcp_stubs(monkeypatch):
    state = {"scaffolds": {}, "scaffolds_by_id": {}, "designs": {}}

    async def fake_get_spec(category, variant):
        return _postcard_6x9() if (category, variant) == ("postcard", "6x9") else None

    from app.dmaas import service

    monkeypatch.setattr(service, "get_spec", fake_get_spec)

    async def fake_insert_scaffold(**kwargs):
        s = Scaffold(
            id=uuid4(),
            slug=kwargs["slug"],
            name=kwargs["name"],
            description=kwargs.get("description"),
            format=kwargs["format"],
            compatible_specs=kwargs["compatible_specs"],
            prop_schema=kwargs["prop_schema"],
            constraint_specification=kwargs["constraint_specification"],
            preview_image_url=kwargs.get("preview_image_url"),
            vertical_tags=kwargs.get("vertical_tags", []),
            is_active=kwargs.get("is_active", True),
            version_number=kwargs.get("version_number", 1),
            created_by_user_id=kwargs.get("created_by_user_id"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["scaffolds"][s.slug] = s
        state["scaffolds_by_id"][s.id] = s
        return s

    async def fake_get_scaffold_by_slug(slug):
        return state["scaffolds"].get(slug)

    async def fake_get_scaffold_by_id(sid):
        return state["scaffolds_by_id"].get(sid)

    async def fake_list_scaffolds(*, format=None, vertical=None, spec_category=None, active_only=True):
        out = list(state["scaffolds"].values())
        if format:
            out = [s for s in out if s.format == format]
        return out

    async def fake_update_scaffold(*, slug, fields):
        s = state["scaffolds"].get(slug)
        if not s:
            return None
        for k, v in fields.items():
            setattr(s, k, v)
        return s

    async def fake_insert_design(**kwargs):
        d = Design(
            id=uuid4(),
            scaffold_id=kwargs["scaffold_id"],
            spec_category=kwargs["spec_category"],
            spec_variant=kwargs["spec_variant"],
            content_config=kwargs["content_config"],
            resolved_positions=kwargs["resolved_positions"],
            brand_id=kwargs.get("brand_id"),
            audience_template_id=kwargs.get("audience_template_id"),
            created_by_user_id=kwargs.get("created_by_user_id"),
            version_number=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["designs"][d.id] = d
        return d

    async def fake_get_design(design_id):
        return state["designs"].get(design_id)

    async def fake_update_design_content(*, design_id, content_config, resolved_positions):
        d = state["designs"].get(design_id)
        if not d:
            return None
        d.content_config = content_config
        d.resolved_positions = resolved_positions
        d.version_number += 1
        return d

    monkeypatch.setattr(repo, "insert_scaffold", fake_insert_scaffold)
    monkeypatch.setattr(repo, "get_scaffold_by_slug", fake_get_scaffold_by_slug)
    monkeypatch.setattr(repo, "get_scaffold_by_id", fake_get_scaffold_by_id)
    monkeypatch.setattr(repo, "list_scaffolds", fake_list_scaffolds)
    monkeypatch.setattr(repo, "update_scaffold", fake_update_scaffold)
    monkeypatch.setattr(repo, "insert_design", fake_insert_design)
    monkeypatch.setattr(repo, "get_design", fake_get_design)
    monkeypatch.setattr(repo, "update_design_content", fake_update_design_content)
    monkeypatch.setattr(mcp_module.repo, "insert_scaffold", fake_insert_scaffold)
    monkeypatch.setattr(mcp_module.repo, "get_scaffold_by_slug", fake_get_scaffold_by_slug)
    monkeypatch.setattr(mcp_module.repo, "get_scaffold_by_id", fake_get_scaffold_by_id)
    monkeypatch.setattr(mcp_module.repo, "list_scaffolds", fake_list_scaffolds)
    monkeypatch.setattr(mcp_module.repo, "update_scaffold", fake_update_scaffold)
    monkeypatch.setattr(mcp_module.repo, "insert_design", fake_insert_design)
    monkeypatch.setattr(mcp_module.repo, "get_design", fake_get_design)
    monkeypatch.setattr(mcp_module.repo, "update_design_content", fake_update_design_content)

    return state


async def _call_tool(name: str, /, **kwargs):
    """FastMCP 3.x API: mcp.get_tool(name) returns a FunctionTool whose
    `.fn` is the raw async callable."""
    tool = await mcp_module.mcp.get_tool(name)
    return await tool.fn(**kwargs)


async def _all_tool_names() -> set[str]:
    tools = await mcp_module.mcp.list_tools()
    return {t.name for t in tools}


@pytest.mark.asyncio
async def test_all_required_tools_registered():
    expected = {
        "list_specs",
        "get_spec",
        "list_scaffolds",
        "get_scaffold",
        "validate_constraints",
        "preview_scaffold",
        "create_scaffold",
        "update_scaffold",
        "create_design",
        "get_design",
        "update_design_content",
        "validate_design",
    }
    names = await _all_tool_names()
    missing = expected - names
    assert not missing, f"missing MCP tools: {missing}"


@pytest.mark.asyncio
async def test_list_specs_tool(monkeypatch):
    """list_specs returns a catalog the agent can browse before calling get_spec."""
    from app.mcp import dmaas as mcp_module

    rows = [_postcard_6x9()]

    async def fake_list_specs(category=None):
        return [r for r in rows if category is None or r.mailer_category == category]

    monkeypatch.setattr(mcp_module.direct_mail_specs, "list_specs", fake_list_specs)

    out = await _call_tool("list_specs")
    assert out["count"] == 1
    item = out["specs"][0]
    assert item["mailer_category"] == "postcard"
    assert item["variant"] == "6x9"
    assert item["bleed_w_in"] == 6.25
    assert "label" in item


@pytest.mark.asyncio
async def test_list_specs_category_filter(monkeypatch):
    from app.mcp import dmaas as mcp_module

    rows = [_postcard_6x9()]

    async def fake_list_specs(category=None):
        return [r for r in rows if category is None or r.mailer_category == category]

    monkeypatch.setattr(mcp_module.direct_mail_specs, "list_specs", fake_list_specs)
    out = await _call_tool("list_specs", category="letter")
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_get_spec_tool_returns_zones_and_regions(monkeypatch):
    """get_spec returns the resolved binding — every named zone the
    scaffold-authoring agent will reference in DSL constraints."""
    from app.dmaas import service as service_module

    spec_row = _postcard_6x9()
    spec_row.faces = [
        {
            "name": "front",
            "is_addressable": False,
            "zones": [
                {"name": "usps_scan_warning", "type": "informational",
                 "rect_in": {"w_full_face": True, "h": 2.375, "from_bottom": 0.0},
                 "source": "lob_help_center"},
            ],
        },
        {
            "name": "back",
            "is_addressable": True,
            "zones": [
                {"name": "address_block", "type": "address_block",
                 "rect_in": {"w": 3.5, "h": 1.5, "from_right": 0.525, "from_bottom": 0.875},
                 "source": "usps_dmm"},
                {"name": "postage_indicia", "type": "postage",
                 "rect_in": {"w": 1.0, "h": 1.0, "from_right": 0.25, "from_top": 0.25},
                 "source": "usps_dmm"},
            ],
        },
    ]

    async def fake_get_spec(category, variant):
        return spec_row if (category, variant) == ("postcard", "6x9") else None

    monkeypatch.setattr(service_module, "get_spec", fake_get_spec)

    out = await _call_tool("get_spec", category="postcard", variant="6x9")
    assert "zones" in out
    assert "regions" in out
    assert "back_address_block" in out["zones"]
    assert "back_postage_indicia" in out["zones"]
    assert "front_usps_scan_warning" in out["zones"]
    region_types = {r["type"] for r in out["regions"]}
    assert "address_block" in region_types
    assert "postage" in region_types


@pytest.mark.asyncio
async def test_get_spec_unknown_returns_error(monkeypatch):
    from app.dmaas import service as service_module

    async def fake_get_spec(category, variant):
        return None

    monkeypatch.setattr(service_module, "get_spec", fake_get_spec)
    out = await _call_tool("get_spec", category="postcard", variant="99x99")
    assert out["error"] == "spec_not_found"


@pytest.mark.asyncio
async def test_validate_constraints_tool(mcp_stubs):
    out = await _call_tool(
        "validate_constraints",
        spec_category="postcard",
        spec_variant="6x9",
        constraint_specification=_hero_spec(),
        sample_content={
            "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
            "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
        },
    )
    assert out["is_valid"] is True
    assert "headline" in out["positions"]
    assert "safe_zone" in out["zones"]


@pytest.mark.asyncio
async def test_validate_constraints_unknown_spec_returns_error(mcp_stubs):
    out = await _call_tool(
        "validate_constraints",
        spec_category="postcard",
        spec_variant="99x99",
        constraint_specification=_hero_spec(),
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_create_then_get_scaffold(mcp_stubs):
    created = await _call_tool(
        "create_scaffold",
        slug="t",
        name="T",
        format="postcard",
        constraint_specification=_hero_spec(),
        compatible_specs=[{"category": "postcard", "variant": "6x9"}],
        placeholder_content={
            "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
            "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
        },
    )
    assert created["slug"] == "t"
    fetched = await _call_tool("get_scaffold", slug="t")
    assert fetched["slug"] == "t"


@pytest.mark.asyncio
async def test_create_scaffold_rejects_unsolvable(mcp_stubs):
    out = await _call_tool(
        "create_scaffold",
        slug="bad",
        name="B",
        format="postcard",
        compatible_specs=[{"category": "postcard", "variant": "6x9"}],
        constraint_specification={
            "elements": ["a"],
            "zones": ["safe_zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "safe_zone"},
                {"type": "min_size", "element": "a", "min_width": 99999, "strength": "required"},
            ],
        },
    )
    assert out["error"] == "scaffold_does_not_solve"


@pytest.mark.asyncio
async def test_create_design_via_mcp(mcp_stubs):
    sc = await _call_tool(
        "create_scaffold",
        slug="d",
        name="D",
        format="postcard",
        constraint_specification=_hero_spec(),
        compatible_specs=[{"category": "postcard", "variant": "6x9"}],
        placeholder_content={
            "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
            "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
        },
    )
    design = await _call_tool(
        "create_design",
        scaffold_id=sc["id"],
        spec_category="postcard",
        spec_variant="6x9",
        content_config={
            "headline": {
                "text": "Hi",
                "intrinsic": {"preferred_width": 1200, "preferred_height": 140},
            },
            "cta": {
                "text": "Click",
                "intrinsic": {"preferred_width": 600, "preferred_height": 80},
            },
        },
    )
    assert "id" in design
    assert "headline" in design["resolved_positions"]


@pytest.mark.asyncio
async def test_get_scaffold_404_by_unknown_slug(mcp_stubs):
    out = await _call_tool("get_scaffold", slug="nope")
    assert out["error"] == "scaffold_not_found"
