"""REST endpoint tests for /api/v1/dmaas/*.

Auth is bypassed via dependency_overrides; repository is monkeypatched to
in-memory state. Spec lookups go through `direct_mail.specs.get_spec`,
which we also stub so the test suite has no DB dependency."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.direct_mail.specs import MailerSpec
from app.dmaas import repository as repo
from app.dmaas.repository import AuthoringSession, Design, Scaffold
from app.main import app
from app.routers import dmaas as dmaas_router

_OPERATOR = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    email="op@example.com",
    platform_role="platform_operator",
    active_organization_id=None,
    org_role=None,
    role="operator",
    client_id=None,
)
_CLIENT = UserContext(
    auth_user_id=UUID("33333333-3333-3333-3333-333333333333"),
    business_user_id=UUID("44444444-4444-4444-4444-444444444444"),
    email="client@example.com",
    platform_role=None,
    active_organization_id=None,
    org_role="member",
    role="client",
    client_id=UUID("55555555-5555-5555-5555-555555555555"),
)


@pytest.fixture
def auth_operator():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _OPERATOR
    app.dependency_overrides[require_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client():
    """Standard client (non-operator) — for testing operator-gated routes
    return 403."""
    from fastapi import HTTPException, status

    def _deny():
        raise HTTPException(403, {"error": "operator_role_required"})

    app.dependency_overrides[verify_supabase_jwt] = lambda: _CLIENT
    app.dependency_overrides[require_operator] = _deny
    yield
    app.dependency_overrides.clear()


def _postcard_6x9_spec() -> MailerSpec:
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
        zones={
            "ink_free": {
                "panel": "back",
                "w_in": 4.0,
                "h_in": 2.375,
                "from_right_in": 0.275,
                "from_bottom_in": 0.25,
            }
        },
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


def _hero_constraint_spec() -> dict:
    return {
        "elements": ["headline", "cta"],
        "zones": ["safe_zone"],
        "constraints": [
            {"type": "inside", "element": "headline", "zone": "safe_zone"},
            {"type": "inside", "element": "cta", "zone": "safe_zone"},
            {"type": "anchor", "element": "headline", "position": "top_center", "reference": "safe_zone", "margin": 80},
            {"type": "anchor", "element": "cta", "position": "bottom_center", "reference": "safe_zone", "margin": 100},
            {"type": "no_overlap", "elements": ["headline", "cta"]},
        ],
    }


@pytest.fixture
def stub_specs(monkeypatch):
    """Stub the spec lookup so we don't need a real DB."""

    async def fake_get_spec(category, variant):
        if (category, variant) == ("postcard", "6x9"):
            return _postcard_6x9_spec()
        return None

    # The service module imports get_spec at import-time; patch where it's used.
    from app.dmaas import service

    monkeypatch.setattr(service, "get_spec", fake_get_spec)


@pytest.fixture
def stub_repo(monkeypatch):
    """In-memory replacement for app.dmaas.repository functions."""
    state = {
        "scaffolds": {},  # slug -> Scaffold
        "scaffolds_by_id": {},
        "designs": {},  # id -> Design
        "authoring": [],
    }

    def _make_scaffold(**kwargs) -> Scaffold:
        sid = kwargs.pop("id", uuid4())
        return Scaffold(
            id=sid,
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

    async def fake_list_scaffolds(*, format=None, vertical=None, spec_category=None, active_only=True):
        out = list(state["scaffolds"].values())
        if format:
            out = [s for s in out if s.format == format]
        if vertical:
            out = [s for s in out if vertical in s.vertical_tags]
        if spec_category:
            out = [s for s in out if any(cs.get("category") == spec_category for cs in s.compatible_specs)]
        if active_only:
            out = [s for s in out if s.is_active]
        return out

    async def fake_get_scaffold_by_slug(slug):
        return state["scaffolds"].get(slug)

    async def fake_get_scaffold_by_id(sid):
        return state["scaffolds_by_id"].get(sid)

    async def fake_insert_scaffold(**kwargs):
        s = _make_scaffold(**kwargs)
        state["scaffolds"][s.slug] = s
        state["scaffolds_by_id"][s.id] = s
        return s

    async def fake_update_scaffold(*, slug, fields):
        s = state["scaffolds"].get(slug)
        if not s:
            return None
        for k, v in fields.items():
            setattr(s, k, v)
        s.updated_at = datetime.now(UTC)
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
        d.updated_at = datetime.now(UTC)
        return d

    async def fake_list_designs(*, brand_id=None, audience_template_id=None, scaffold_id=None, limit=50):
        out = list(state["designs"].values())
        if brand_id:
            out = [d for d in out if d.brand_id == brand_id]
        if scaffold_id:
            out = [d for d in out if d.scaffold_id == scaffold_id]
        return out[:limit]

    async def fake_insert_authoring(**kwargs):
        a = AuthoringSession(
            id=uuid4(),
            scaffold_id=kwargs.get("scaffold_id"),
            prompt=kwargs["prompt"],
            proposed_constraint_specification=kwargs["proposed_constraint_specification"],
            accepted=kwargs.get("accepted", False),
            notes=kwargs.get("notes"),
            created_by_user_id=kwargs.get("created_by_user_id"),
            created_at=datetime.now(UTC),
        )
        state["authoring"].append(a)
        return a

    async def fake_list_authoring(*, limit=50):
        return list(state["authoring"])[:limit]

    monkeypatch.setattr(repo, "list_scaffolds", fake_list_scaffolds)
    monkeypatch.setattr(repo, "get_scaffold_by_slug", fake_get_scaffold_by_slug)
    monkeypatch.setattr(repo, "get_scaffold_by_id", fake_get_scaffold_by_id)
    monkeypatch.setattr(repo, "insert_scaffold", fake_insert_scaffold)
    monkeypatch.setattr(repo, "update_scaffold", fake_update_scaffold)
    monkeypatch.setattr(repo, "insert_design", fake_insert_design)
    monkeypatch.setattr(repo, "get_design", fake_get_design)
    monkeypatch.setattr(repo, "update_design_content", fake_update_design_content)
    monkeypatch.setattr(repo, "list_designs", fake_list_designs)
    monkeypatch.setattr(repo, "insert_authoring_session", fake_insert_authoring)
    monkeypatch.setattr(repo, "list_authoring_sessions", fake_list_authoring)

    # The router imports `repo` aliased; patch through the module too.
    monkeypatch.setattr(dmaas_router.repo, "list_scaffolds", fake_list_scaffolds)
    monkeypatch.setattr(dmaas_router.repo, "get_scaffold_by_slug", fake_get_scaffold_by_slug)
    monkeypatch.setattr(dmaas_router.repo, "get_scaffold_by_id", fake_get_scaffold_by_id)
    monkeypatch.setattr(dmaas_router.repo, "insert_scaffold", fake_insert_scaffold)
    monkeypatch.setattr(dmaas_router.repo, "update_scaffold", fake_update_scaffold)
    monkeypatch.setattr(dmaas_router.repo, "insert_design", fake_insert_design)
    monkeypatch.setattr(dmaas_router.repo, "get_design", fake_get_design)
    monkeypatch.setattr(dmaas_router.repo, "update_design_content", fake_update_design_content)
    monkeypatch.setattr(dmaas_router.repo, "list_designs", fake_list_designs)
    monkeypatch.setattr(dmaas_router.repo, "insert_authoring_session", fake_insert_authoring)
    monkeypatch.setattr(dmaas_router.repo, "list_authoring_sessions", fake_list_authoring)
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Validate-constraints (the agent's tightest loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_constraints_passes(auth_operator, stub_specs, stub_repo):
    body = {
        "spec_category": "postcard",
        "spec_variant": "6x9",
        "constraint_specification": _hero_constraint_spec(),
        "sample_content": {
            "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
            "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
        },
    }
    async with await _client() as c:
        r = await c.post("/api/v1/dmaas/scaffolds/validate-constraints", json=body)
    assert r.status_code == 200
    payload = r.json()
    assert payload["is_valid"]
    assert "headline" in payload["positions"]
    # Validate response also returns the resolved zone geometry — the
    # frontend renders these as guides.
    assert "safe_zone" in payload["zones"]


@pytest.mark.asyncio
async def test_validate_constraints_returns_conflicts(auth_operator, stub_specs, stub_repo):
    bad_spec = {
        "elements": ["a"],
        "zones": ["safe_zone"],
        "constraints": [
            {"type": "inside", "element": "a", "zone": "safe_zone"},
            # Force unsatisfiable: required min_width > zone width.
            {"type": "min_size", "element": "a", "min_width": 999999, "strength": "required"},
        ],
    }
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffolds/validate-constraints",
            json={
                "spec_category": "postcard",
                "spec_variant": "6x9",
                "constraint_specification": bad_spec,
            },
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["is_valid"] is False
    assert any(c["phase"] == "linear" for c in payload["conflicts"])


@pytest.mark.asyncio
async def test_validate_constraints_unknown_spec_400(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffolds/validate-constraints",
            json={
                "spec_category": "postcard",
                "spec_variant": "99x99",
                "constraint_specification": _hero_constraint_spec(),
            },
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_validate_constraints_invalid_dsl_400(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffolds/validate-constraints",
            json={
                "spec_category": "postcard",
                "spec_variant": "6x9",
                "constraint_specification": {"elements": [], "constraints": []},
            },
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Scaffold create / read / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_scaffold_happy_path(auth_operator, stub_specs, stub_repo):
    body = {
        "slug": "my-scaffold",
        "name": "My Scaffold",
        "format": "postcard",
        "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
        "constraint_specification": _hero_constraint_spec(),
        "placeholder_content": {
            "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
            "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
        },
    }
    async with await _client() as c:
        r = await c.post("/api/v1/dmaas/scaffolds", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "my-scaffold"
    assert stub_repo["scaffolds"]["my-scaffold"].is_active


@pytest.mark.asyncio
async def test_create_scaffold_rejects_unsolvable(auth_operator, stub_specs, stub_repo):
    bad = {
        "slug": "broken",
        "name": "Broken",
        "format": "postcard",
        "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
        "constraint_specification": {
            "elements": ["a"],
            "zones": ["safe_zone"],
            "constraints": [
                {"type": "inside", "element": "a", "zone": "safe_zone"},
                {"type": "min_size", "element": "a", "min_width": 99999, "strength": "required"},
            ],
        },
    }
    async with await _client() as c:
        r = await c.post("/api/v1/dmaas/scaffolds", json=bad)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "scaffold_does_not_solve"
    assert "broken" not in stub_repo["scaffolds"]


@pytest.mark.asyncio
async def test_create_scaffold_requires_operator(auth_client, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffolds",
            json={
                "slug": "x",
                "name": "X",
                "format": "postcard",
                "compatible_specs": [],
                "constraint_specification": _hero_constraint_spec(),
            },
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_scaffold_404(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.get("/api/v1/dmaas/scaffolds/missing")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_scaffolds_filters_by_format(auth_operator, stub_specs, stub_repo):
    # Insert via the create endpoint (also exercises that path).
    async with await _client() as c:
        await c.post(
            "/api/v1/dmaas/scaffolds",
            json={
                "slug": "a",
                "name": "A",
                "format": "postcard",
                "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
                "constraint_specification": _hero_constraint_spec(),
                "placeholder_content": {
                    "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
                    "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
                },
            },
        )
        r = await c.get("/api/v1/dmaas/scaffolds?format=postcard")
        body = r.json()
        assert body["count"] == 1
        r2 = await c.get("/api/v1/dmaas/scaffolds?format=letter")
        assert r2.json()["count"] == 0


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_design_happy_path(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        cs = await c.post(
            "/api/v1/dmaas/scaffolds",
            json={
                "slug": "scaf",
                "name": "Scaf",
                "format": "postcard",
                "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
                "constraint_specification": _hero_constraint_spec(),
                "placeholder_content": {
                    "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
                    "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
                },
            },
        )
        scaffold_id = cs.json()["id"]

        d = await c.post(
            "/api/v1/dmaas/designs",
            json={
                "scaffold_id": scaffold_id,
                "spec_category": "postcard",
                "spec_variant": "6x9",
                "content_config": {
                    "headline": {
                        "text": "Hi",
                        "intrinsic": {"preferred_width": 1200, "preferred_height": 140},
                    },
                    "cta": {
                        "text": "Click",
                        "intrinsic": {"preferred_width": 600, "preferred_height": 80},
                    },
                },
            },
        )
    assert d.status_code == 201, d.text
    body = d.json()
    assert "headline" in body["resolved_positions"]
    assert "cta" in body["resolved_positions"]


@pytest.mark.asyncio
async def test_validate_design_endpoint(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        cs = await c.post(
            "/api/v1/dmaas/scaffolds",
            json={
                "slug": "v",
                "name": "V",
                "format": "postcard",
                "compatible_specs": [{"category": "postcard", "variant": "6x9"}],
                "constraint_specification": _hero_constraint_spec(),
                "placeholder_content": {
                    "headline": {"intrinsic": {"preferred_width": 1200, "preferred_height": 140}},
                    "cta": {"intrinsic": {"preferred_width": 600, "preferred_height": 80}},
                },
            },
        )
        d = await c.post(
            "/api/v1/dmaas/designs",
            json={
                "scaffold_id": cs.json()["id"],
                "spec_category": "postcard",
                "spec_variant": "6x9",
                "content_config": {
                    "headline": {
                        "text": "Hi",
                        "intrinsic": {"preferred_width": 1200, "preferred_height": 140},
                    },
                    "cta": {
                        "text": "Click",
                        "intrinsic": {"preferred_width": 600, "preferred_height": 80},
                    },
                },
            },
        )
        design_id = d.json()["id"]
        v = await c.post(f"/api/v1/dmaas/designs/{design_id}/validate")
    assert v.status_code == 200
    assert v.json()["is_valid"]


# ---------------------------------------------------------------------------
# Authoring sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_authoring_session(auth_operator, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffold-authoring-sessions",
            json={
                "prompt": "Hero headline scaffold",
                "proposed_constraint_specification": _hero_constraint_spec(),
                "accepted": False,
            },
        )
    assert r.status_code == 201
    assert r.json()["accepted"] is False


@pytest.mark.asyncio
async def test_authoring_sessions_require_operator(auth_client, stub_specs, stub_repo):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/dmaas/scaffold-authoring-sessions",
            json={
                "prompt": "x",
                "proposed_constraint_specification": _hero_constraint_spec(),
            },
        )
    assert r.status_code == 403
