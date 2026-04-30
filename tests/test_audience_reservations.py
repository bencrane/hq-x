"""Audience reservations router tests.

Auth uses FastAPI dependency_overrides. DB-touching paths use an in-memory
fake of `get_db_connection` so the full reservation lifecycle (idempotent
upsert, cross-org isolation) can be exercised without a real DB. The DEX
client is patched at the module seam in `audience_reservations` so we can
exercise the descriptor / count / preview fan-out paths deterministically.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.routers import audience_reservations as res_router
from app.services import dex_client


ORG_A = UUID("33333333-3333-3333-3333-333333333333")
ORG_B = UUID("44444444-4444-4444-4444-444444444444")


def _user(auth_id: str, biz_id: str, org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=UUID(auth_id),
        business_user_id=UUID(biz_id),
        email=f"{auth_id[:4]}@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role="owner" if org_id else None,
        role="operator",
        client_id=None,
    )


USER_A = _user(
    "11111111-1111-1111-1111-111111111111",
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ORG_A,
)
USER_B = _user(
    "22222222-2222-2222-2222-222222222222",
    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ORG_B,
)
USER_NO_ORG = _user(
    "55555555-5555-5555-5555-555555555555",
    "cccccccc-cccc-cccc-cccc-cccccccccccc",
    None,
)


# ─────────────────────── in-memory fake of get_db_connection ───────────────


_COLS = [
    "id", "organization_id", "data_engine_audience_id",
    "source_template_slug", "source_template_id", "audience_name",
    "status", "reserved_at", "reserved_by_user_id",
    "notes", "metadata", "created_at", "updated_at",
]


class _FakeCursor:
    def __init__(self, store: dict[UUID, dict[str, Any]]):
        self._store = store
        self.description: list[tuple] | None = None
        self._row: tuple | None = None
        self._rows: list[tuple] = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql: str, params: tuple) -> None:
        sql_n = " ".join(sql.split())
        if sql_n.startswith("INSERT INTO business.org_audience_reservations"):
            (
                org_id, audience_id, slug, tpl_id, name, biz_user_id,
                notes, metadata_json,
            ) = params
            now = datetime.now(tz=timezone.utc)
            existing_id = None
            for k, r in self._store.items():
                if (
                    r["organization_id"] == UUID(org_id)
                    and r["data_engine_audience_id"] == UUID(audience_id)
                ):
                    existing_id = k
                    break
            if existing_id is not None:
                row = self._store[existing_id]
                row["notes"] = notes
                row["metadata"] = json.loads(metadata_json)
                row["updated_at"] = now
                self._set_row(row)
                return
            row = {
                "id": uuid4(),
                "organization_id": UUID(org_id),
                "data_engine_audience_id": UUID(audience_id),
                "source_template_slug": slug,
                "source_template_id": UUID(tpl_id),
                "audience_name": name,
                "status": "reserved",
                "reserved_at": now,
                "reserved_by_user_id": UUID(biz_user_id) if biz_user_id else None,
                "notes": notes,
                "metadata": json.loads(metadata_json),
                "created_at": now,
                "updated_at": now,
            }
            self._store[row["id"]] = row
            self._set_row(row)
        elif (
            sql_n.startswith("SELECT")
            and "WHERE organization_id = %s" in sql_n
            and "ORDER BY reserved_at" in sql_n
        ):
            org_id, limit, offset = params
            rows = sorted(
                [r for r in self._store.values()
                 if r["organization_id"] == UUID(org_id)],
                key=lambda r: r["reserved_at"],
                reverse=True,
            )[offset: offset + limit]
            self._set_rows(rows)
        elif (
            sql_n.startswith("SELECT")
            and "WHERE id = %s AND organization_id = %s" in sql_n
        ):
            res_id, org_id = params
            row = self._store.get(UUID(res_id))
            if row and row["organization_id"] == UUID(org_id):
                self._set_row(row)
            else:
                self._set_row(None)
        else:
            raise AssertionError(f"unhandled SQL: {sql_n}")

    def _set_row(self, row: dict[str, Any] | None) -> None:
        self.description = [(c,) for c in _COLS]
        if row is None:
            self._row = None
            return
        self._row = tuple(row[c] for c in _COLS)

    def _set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.description = [(c,) for c in _COLS]
        self._rows = [tuple(r[c] for c in _COLS) for r in rows]

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    async def commit(self):
        return None


@pytest.fixture
def fake_db(monkeypatch):
    store: dict[UUID, dict[str, Any]] = {}

    @asynccontextmanager
    async def fake_get_db():
        yield _FakeConn(store)

    monkeypatch.setattr(res_router, "get_db_connection", fake_get_db)
    return store


# ─────────────────────── DEX client stubs ──────────────────────────


_TEMPLATE_ID = UUID("99999999-9999-9999-9999-999999999999")
_DESCRIPTOR_OK = {
    "spec": {
        "id": "00000000-0000-0000-0000-000000000abc",
        "template_id": str(_TEMPLATE_ID),
        "name": "DAT — fast-growing carriers (prototype)",
        "filter_overrides": {},
        "resolved_filters": {"limit": 100, "offset": 0},
        "created_by_user_id": None,
        "created_at": "2026-04-30T12:00:00",
    },
    "template": {
        "id": str(_TEMPLATE_ID),
        "slug": "motor-carriers-new-entrants-90d",
        "name": "Motor carriers — new entrants (90d)",
        "description": "Fast-growing motor carriers in their first 90 days.",
        "source_endpoint": "/api/v1/fmcsa/audiences/new-entrants-90d",
        "default_filters": {"limit": 100, "offset": 0},
        "attribute_schema": {"limit": {"type": "integer"}},
        "partner_types": ["factoring_company"],
    },
    "audience_attributes": [
        {"key": "limit", "value": 100, "schema": {"type": "integer"}},
    ],
}

_COUNT_OK = {
    "total": 412,
    "mv_sources": [{"name": "mv_fmcsa_new_carriers_90d"}],
    "generated_at": "2026-04-30T12:00:00Z",
}

_PREVIEW_OK = {
    "items": [
        {"dot_number": "1234567", "legal_name": "ACME TRUCKING LLC"},
    ],
    "total": 412,
    "has_more": True,
    "limit": 50,
    "offset": 0,
    "mv_sources": [],
    "generated_at": "2026-04-30T12:00:00Z",
}


@pytest.fixture
def dex_ok(monkeypatch):
    calls: dict[str, list[Any]] = {
        "descriptor": [], "count": [], "preview": [],
    }

    async def fake_descriptor(spec_id, *, bearer_token=None):
        calls["descriptor"].append({"spec_id": spec_id, "bearer": bearer_token})
        return _DESCRIPTOR_OK

    async def fake_count(spec_id, *, bearer_token=None):
        calls["count"].append({"spec_id": spec_id, "bearer": bearer_token})
        return _COUNT_OK

    async def fake_preview(spec_id, *, limit=50, offset=0, bearer_token=None):
        calls["preview"].append(
            {"spec_id": spec_id, "limit": limit, "offset": offset, "bearer": bearer_token}
        )
        return {**_PREVIEW_OK, "limit": limit, "offset": offset}

    monkeypatch.setattr(dex_client, "get_audience_descriptor", fake_descriptor)
    monkeypatch.setattr(dex_client, "count_audience_members", fake_count)
    monkeypatch.setattr(dex_client, "list_audience_members", fake_preview)
    return calls


# ─────────────────────── auth fixtures ──────────────────────────


@pytest.fixture
def as_user_a():
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_A
    yield USER_A
    app.dependency_overrides.clear()


@pytest.fixture
def as_user_b():
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_B
    yield USER_B
    app.dependency_overrides.clear()


@pytest.fixture
def as_user_no_org():
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_NO_ORG
    yield USER_NO_ORG
    app.dependency_overrides.clear()


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


_AUDIENCE_ID = "12121212-1212-1212-1212-121212121212"


# ─────────────────────── tests ──────────────────────────


async def test_create_reservation_happy_path(fake_db, dex_ok, as_user_a):
    body = {"data_engine_audience_id": _AUDIENCE_ID, "notes": "first try"}
    async with _client() as c:
        resp = await c.post("/api/audience-reservations", json=body)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["organization_id"] == str(ORG_A)
    assert payload["data_engine_audience_id"] == _AUDIENCE_ID
    assert payload["source_template_slug"] == "motor-carriers-new-entrants-90d"
    assert payload["source_template_id"] == str(_TEMPLATE_ID)
    assert payload["audience_name"].startswith("DAT —")
    assert payload["notes"] == "first try"
    assert payload["status"] == "reserved"
    # Database has exactly one row.
    assert len(fake_db) == 1


async def test_create_reservation_idempotent_same_audience(fake_db, dex_ok, as_user_a):
    body = {"data_engine_audience_id": _AUDIENCE_ID, "notes": "first"}
    async with _client() as c:
        resp1 = await c.post("/api/audience-reservations", json=body)
        assert resp1.status_code == 201
        first_id = resp1.json()["id"]

        body2 = {"data_engine_audience_id": _AUDIENCE_ID, "notes": "second"}
        resp2 = await c.post("/api/audience-reservations", json=body2)
        assert resp2.status_code == 201
        assert resp2.json()["id"] == first_id
        assert resp2.json()["notes"] == "second"
    assert len(fake_db) == 1


async def test_create_reservation_unknown_audience_returns_404(
    fake_db, monkeypatch, as_user_a,
):
    async def fake_descriptor(spec_id, *, bearer_token=None):
        raise dex_client.DexCallError(404, {"error": "audience spec not found"})

    monkeypatch.setattr(dex_client, "get_audience_descriptor", fake_descriptor)

    body = {"data_engine_audience_id": _AUDIENCE_ID}
    async with _client() as c:
        resp = await c.post("/api/audience-reservations", json=body)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "audience_not_found"
    assert len(fake_db) == 0


async def test_create_reservation_dex_unreachable_returns_502(
    fake_db, monkeypatch, as_user_a,
):
    async def fake_descriptor(spec_id, *, bearer_token=None):
        raise dex_client.DexCallError(599, "connection refused")

    monkeypatch.setattr(dex_client, "get_audience_descriptor", fake_descriptor)

    body = {"data_engine_audience_id": _AUDIENCE_ID}
    async with _client() as c:
        resp = await c.post("/api/audience-reservations", json=body)
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "dex_call_failed"


async def test_get_reservation_cross_org_returns_404(fake_db, dex_ok):
    # User A creates a reservation.
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_A
    async with _client() as c:
        resp = await c.post(
            "/api/audience-reservations",
            json={"data_engine_audience_id": _AUDIENCE_ID},
        )
        reservation_id = resp.json()["id"]
    app.dependency_overrides.clear()

    # User B (different org) cannot see it.
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_B
    async with _client() as c:
        resp = await c.get(f"/api/audience-reservations/{reservation_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "reservation_not_found"

        resp = await c.get("/api/audience-reservations")
        assert resp.json() == []
    app.dependency_overrides.clear()


async def test_composite_audience_endpoint_fans_out(fake_db, dex_ok, as_user_a):
    async with _client() as c:
        create = await c.post(
            "/api/audience-reservations",
            json={"data_engine_audience_id": _AUDIENCE_ID},
        )
        rid = create.json()["id"]

        resp = await c.get(f"/api/audience-reservations/{rid}/audience")
        assert resp.status_code == 200
        body = resp.json()
        assert body["reservation"]["id"] == rid
        assert body["descriptor"]["template"]["slug"] == "motor-carriers-new-entrants-90d"
        assert body["count"]["total"] == 412

    # Composite called BOTH descriptor and count (1 each from the create
    # path, then 1 each from the composite path).
    assert len(dex_ok["descriptor"]) == 2
    assert len(dex_ok["count"]) == 1


async def test_members_pagination_passthrough(fake_db, dex_ok, as_user_a):
    async with _client() as c:
        create = await c.post(
            "/api/audience-reservations",
            json={"data_engine_audience_id": _AUDIENCE_ID},
        )
        rid = create.json()["id"]

        resp = await c.get(
            f"/api/audience-reservations/{rid}/members",
            params={"limit": 25, "offset": 50},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 25
        assert body["offset"] == 50

    assert dex_ok["preview"][-1]["limit"] == 25
    assert dex_ok["preview"][-1]["offset"] == 50


async def test_no_active_org_returns_400_on_create(fake_db, dex_ok, as_user_no_org):
    body = {"data_engine_audience_id": _AUDIENCE_ID}
    async with _client() as c:
        resp = await c.post("/api/audience-reservations", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_required"


async def test_no_active_org_returns_400_on_list(fake_db, dex_ok, as_user_no_org):
    async with _client() as c:
        resp = await c.get("/api/audience-reservations")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_required"
