"""brand_domains service tests against a queue-driven Postgres fake.

Covers register/list/delete idempotency, cross-org isolation, JSON
roundtripping, and the entri stamping side-effect when binding a
landing-page domain.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.config import settings
from app.dmaas import entri_domains as entri_repo
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError
from app.services import brand_domains as svc

_ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_BRAND = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_ENTRI_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


# ── Fake DB ──────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, queue: list[Any], capture: list[dict[str, Any]]):
        self._queue = queue
        self._capture = capture
        self._current: Any = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._capture.append({"sql": sql, "params": params})
        if self._queue:
            self._current = self._queue.pop(0)
        else:
            self._current = None

    async def fetchone(self):
        if isinstance(self._current, list):
            return self._current[0] if self._current else None
        return self._current

    async def fetchall(self):
        if isinstance(self._current, list):
            return self._current
        return [self._current] if self._current else []


class _FakeConn:
    def __init__(self, queue: list[Any], capture: list[dict[str, Any]]):
        self._queue = queue
        self._capture = capture

    def cursor(self):
        return _FakeCursor(self._queue, self._capture)

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


def _patch_db(monkeypatch, queue: list[Any]) -> list[dict[str, Any]]:
    capture: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(queue, capture)

    monkeypatch.setattr(svc, "get_db_connection", _conn)
    return capture


@pytest.fixture
def configured_dub(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", SecretStr("test-dub-key"))
    monkeypatch.setattr(settings, "DUB_API_BASE_URL", None)


# ── get_brand_domain_configs ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_empty_for_brand_with_no_configs(monkeypatch):
    queue: list[Any] = [
        # SELECT dub_domain_config, landing_page_domain_config
        (None, None),
        # existence re-check
        (1,),
    ]
    _patch_db(monkeypatch, queue)
    out = await svc.get_brand_domain_configs(
        brand_id=_BRAND, organization_id=_ORG_A
    )
    assert out.brand_id == _BRAND
    assert out.dub is None
    assert out.landing_page is None


@pytest.mark.asyncio
async def test_get_brand_in_other_org_raises_not_found(monkeypatch):
    queue: list[Any] = [
        # First select returns no row (org filter excluded the brand)
        None,
        # Re-check also returns nothing
        None,
    ]
    _patch_db(monkeypatch, queue)
    with pytest.raises(svc.BrandNotFoundError):
        await svc.get_brand_domain_configs(brand_id=_BRAND, organization_id=_ORG_A)


@pytest.mark.asyncio
async def test_get_returns_existing_dub_binding(monkeypatch):
    dub_jsonb = {
        "domain": "track.acme.com",
        "dub_domain_id": "dom_abc",
        "verified_at": "2026-04-30T12:00:00+00:00",
    }
    queue: list[Any] = [(dub_jsonb, None)]
    _patch_db(monkeypatch, queue)
    out = await svc.get_brand_domain_configs(
        brand_id=_BRAND, organization_id=_ORG_A
    )
    assert out.dub is not None
    assert out.dub.domain == "track.acme.com"
    assert out.dub.dub_domain_id == "dom_abc"
    assert out.landing_page is None


# ── register_dub_domain_for_brand ────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_dub_domain_creates_when_dub_unknown(
    monkeypatch, configured_dub
):
    queue: list[Any] = [
        # get_brand_domain_configs: SELECT — brand exists, no dub binding yet
        (None, None),
        # UPDATE business.brands ...
        None,
    ]
    _patch_db(monkeypatch, queue)

    # Dub workspace doesn't have the domain; create it.
    monkeypatch.setattr(
        dub_client,
        "list_domains",
        lambda **_: [],  # pragma: no cover - covered by get_domain_by_slug below
    )
    monkeypatch.setattr(
        dub_client, "get_domain_by_slug", lambda **_: None
    )
    create_calls: list[dict[str, Any]] = []

    def fake_create(*, api_key, slug, base_url=None, **kw):
        create_calls.append({"slug": slug, "api_key": api_key})
        return {"id": "dom_new", "slug": slug}

    monkeypatch.setattr(dub_client, "create_domain", fake_create)

    binding = await svc.register_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A, domain="track.acme.com"
    )
    assert binding.domain == "track.acme.com"
    assert binding.dub_domain_id == "dom_new"
    assert isinstance(binding.verified_at, datetime)
    assert create_calls == [{"slug": "track.acme.com", "api_key": "test-dub-key"}]


@pytest.mark.asyncio
async def test_register_dub_domain_reuses_existing_dub_object(
    monkeypatch, configured_dub
):
    queue: list[Any] = [
        (None, None),  # get configs: no existing binding
        None,  # UPDATE
    ]
    _patch_db(monkeypatch, queue)

    # Dub already has the domain (e.g. previously registered then deleted
    # locally). Re-use the id without calling create_domain.
    monkeypatch.setattr(
        dub_client,
        "get_domain_by_slug",
        lambda **_: {"id": "dom_existing", "slug": "track.acme.com"},
    )

    def boom(**_):
        raise AssertionError("create_domain should not be called when a row exists")

    monkeypatch.setattr(dub_client, "create_domain", boom)

    binding = await svc.register_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A, domain="track.acme.com"
    )
    assert binding.dub_domain_id == "dom_existing"


@pytest.mark.asyncio
async def test_register_dub_domain_idempotent_when_already_bound(
    monkeypatch, configured_dub
):
    """Re-registering the same domain returns the existing binding without
    touching Dub."""
    existing_jsonb = {
        "domain": "track.acme.com",
        "dub_domain_id": "dom_existing",
        "verified_at": "2026-04-30T12:00:00+00:00",
    }
    queue: list[Any] = [(existing_jsonb, None)]
    _patch_db(monkeypatch, queue)

    def boom(**_):
        raise AssertionError("Dub should not be touched on idempotent re-register")

    monkeypatch.setattr(dub_client, "get_domain_by_slug", boom)
    monkeypatch.setattr(dub_client, "create_domain", boom)

    binding = await svc.register_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A, domain="track.acme.com"
    )
    assert binding.dub_domain_id == "dom_existing"


@pytest.mark.asyncio
async def test_register_dub_domain_raises_on_brand_in_other_org(
    monkeypatch, configured_dub
):
    queue: list[Any] = [None, None]  # configs select empty, re-check empty
    _patch_db(monkeypatch, queue)
    with pytest.raises(svc.BrandNotFoundError):
        await svc.register_dub_domain_for_brand(
            brand_id=_BRAND, organization_id=_ORG_A, domain="track.acme.com"
        )


@pytest.mark.asyncio
async def test_register_dub_domain_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", None)
    with pytest.raises(svc.DubNotConfiguredError):
        await svc.register_dub_domain_for_brand(
            brand_id=_BRAND, organization_id=_ORG_A, domain="track.acme.com"
        )


# ── deregister_dub_domain_for_brand ─────────────────────────────────────


@pytest.mark.asyncio
async def test_deregister_dub_domain_calls_dub_delete(
    monkeypatch, configured_dub
):
    existing = {
        "domain": "track.acme.com",
        "dub_domain_id": "dom_abc",
        "verified_at": "2026-04-30T12:00:00+00:00",
    }
    queue: list[Any] = [(existing, None), None]  # SELECT then UPDATE
    _patch_db(monkeypatch, queue)
    delete_calls: list[str] = []

    def fake_delete(*, api_key, slug, base_url=None, **_):
        delete_calls.append(slug)

    monkeypatch.setattr(dub_client, "delete_domain", fake_delete)

    removed = await svc.deregister_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A
    )
    assert removed is True
    assert delete_calls == ["track.acme.com"]


@pytest.mark.asyncio
async def test_deregister_dub_domain_returns_false_when_no_binding(
    monkeypatch, configured_dub
):
    queue: list[Any] = [(None, None)]  # configs select returns no binding
    _patch_db(monkeypatch, queue)
    removed = await svc.deregister_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A
    )
    assert removed is False


@pytest.mark.asyncio
async def test_deregister_dub_domain_swallows_404(monkeypatch, configured_dub):
    """If Dub returns 404 (already gone there), local row is still removed."""
    existing = {
        "domain": "track.acme.com",
        "dub_domain_id": "dom_abc",
        "verified_at": "2026-04-30T12:00:00+00:00",
    }
    queue: list[Any] = [(existing, None), None]
    _patch_db(monkeypatch, queue)

    def fake_delete(**_):
        raise DubProviderError("not found", status=404, code="not_found")

    monkeypatch.setattr(dub_client, "delete_domain", fake_delete)
    removed = await svc.deregister_dub_domain_for_brand(
        brand_id=_BRAND, organization_id=_ORG_A
    )
    assert removed is True


# ── register_landing_page_domain_for_brand ──────────────────────────────


def _entri_connection(*, organization_id: UUID, domain: str = "pages.acme.com"):

    base = entri_repo.EntriDomainConnection(
        id=_ENTRI_ID,
        organization_id=organization_id,
        channel_campaign_step_id=None,
        domain=domain,
        is_root_domain=False,
        application_url="https://app.example.com/lp/x",
        state="live",
        entri_user_id=str(organization_id),
        entri_token=None,
        entri_token_expires_at=None,
        provider=None,
        setup_type=None,
        propagation_status=None,
        power_status=None,
        secure_status=None,
        last_webhook_id=None,
        last_error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    return base


@pytest.mark.asyncio
async def test_register_landing_page_links_brand_and_stamps_entri(monkeypatch):
    queue: list[Any] = [
        # get_configs: brand has no existing landing-page binding
        (None, None),
        # UPDATE business.brands ...
        None,
        # UPDATE business.entri_domain_connections SET brand_id = ...
        None,
    ]
    _patch_db(monkeypatch, queue)

    async def fake_get_by_id(connection_id):
        assert connection_id == _ENTRI_ID
        return _entri_connection(organization_id=_ORG_A)

    monkeypatch.setattr(entri_repo, "get_by_id", fake_get_by_id)

    binding = await svc.register_landing_page_domain_for_brand(
        brand_id=_BRAND,
        organization_id=_ORG_A,
        entri_connection_id=_ENTRI_ID,
    )
    assert binding.domain == "pages.acme.com"
    assert binding.entri_connection_id == _ENTRI_ID


@pytest.mark.asyncio
async def test_register_landing_page_rejects_cross_org_entri(monkeypatch):
    async def fake_get_by_id(connection_id):
        return _entri_connection(organization_id=_ORG_B)

    monkeypatch.setattr(entri_repo, "get_by_id", fake_get_by_id)

    # Service should reject before any DB write to brands.
    with pytest.raises(svc.EntriConnectionNotFoundError):
        await svc.register_landing_page_domain_for_brand(
            brand_id=_BRAND,
            organization_id=_ORG_A,
            entri_connection_id=_ENTRI_ID,
        )


@pytest.mark.asyncio
async def test_register_landing_page_idempotent_on_same_connection(monkeypatch):
    existing = {
        "domain": "pages.acme.com",
        "entri_connection_id": str(_ENTRI_ID),
        "verified_at": "2026-04-30T12:00:00+00:00",
    }
    queue: list[Any] = [(None, existing)]  # get_configs returns existing landing
    _patch_db(monkeypatch, queue)

    async def fake_get_by_id(connection_id):
        return _entri_connection(organization_id=_ORG_A)

    monkeypatch.setattr(entri_repo, "get_by_id", fake_get_by_id)

    binding = await svc.register_landing_page_domain_for_brand(
        brand_id=_BRAND,
        organization_id=_ORG_A,
        entri_connection_id=_ENTRI_ID,
    )
    assert binding.domain == "pages.acme.com"
    assert binding.entri_connection_id == _ENTRI_ID


# ── get_brand_dub_domain (read helper used by minting) ──────────────────


@pytest.mark.asyncio
async def test_get_brand_dub_domain_returns_string(monkeypatch):
    queue: list[Any] = [({
        "domain": "track.acme.com",
        "dub_domain_id": "dom_x",
        "verified_at": "2026-04-30T12:00:00+00:00",
    },)]
    _patch_db(monkeypatch, queue)
    domain = await svc.get_brand_dub_domain(brand_id=_BRAND)
    assert domain == "track.acme.com"


@pytest.mark.asyncio
async def test_get_brand_dub_domain_returns_none_when_missing(monkeypatch):
    queue: list[Any] = [(None,)]
    _patch_db(monkeypatch, queue)
    domain = await svc.get_brand_dub_domain(brand_id=_BRAND)
    assert domain is None


@pytest.mark.asyncio
async def test_get_brand_dub_domain_returns_none_when_brand_missing(monkeypatch):
    queue: list[Any] = [None]
    _patch_db(monkeypatch, queue)
    domain = await svc.get_brand_dub_domain(brand_id=_BRAND)
    assert domain is None


@pytest.mark.asyncio
async def test_get_brand_landing_page_domain_returns_string(monkeypatch):
    queue: list[Any] = [({
        "domain": "pages.acme.com",
        "entri_connection_id": str(_ENTRI_ID),
        "verified_at": "2026-04-30T12:00:00+00:00",
    },)]
    _patch_db(monkeypatch, queue)
    domain = await svc.get_brand_landing_page_domain(brand_id=_BRAND)
    assert domain == "pages.acme.com"
