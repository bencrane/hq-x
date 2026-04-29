"""Audience drafts router tests.

Auth + validation use FastAPI `dependency_overrides`. DB-touching paths
use an in-memory fake of `get_db_connection` so the full CRUD lifecycle
(including the cross-user 404 case) can be exercised without a real DB.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.routers import audience_drafts as drafts_router

USER_A = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    email="alice@example.com",
    platform_role="platform_operator",
    active_organization_id=None,
    org_role=None,
    role="operator",
    client_id=None,
)
USER_B = UserContext(
    auth_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    business_user_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    email="bob@example.com",
    platform_role="platform_operator",
    active_organization_id=None,
    org_role=None,
    role="operator",
    client_id=None,
)


# ─────────────────────── in-memory fake of get_db_connection ───────────────


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
        if sql_n.startswith("INSERT INTO business.audience_drafts"):
            (
                owner, name, slug, endpoint, overrides_json, resolved_json,
                last_count, last_at,
            ) = params
            now = datetime.now(tz=__import__("datetime").timezone.utc)
            row = {
                "id": uuid4(),
                "created_by_user_id": UUID(owner),
                "name": name,
                "audience_template_slug": slug,
                "source_endpoint": endpoint,
                "filter_overrides": json.loads(overrides_json),
                "resolved_filters": json.loads(resolved_json),
                "last_preview_total_matched": last_count,
                "last_preview_at": last_at,
                "created_at": now,
                "updated_at": now,
            }
            self._store[row["id"]] = row
            self._set_row(row)
        elif sql_n.startswith("SELECT") and "WHERE created_by_user_id = %s" in sql_n and "ORDER BY" in sql_n:
            owner, limit, offset = params
            rows = sorted(
                [r for r in self._store.values() if r["created_by_user_id"] == UUID(owner)],
                key=lambda r: r["created_at"],
                reverse=True,
            )[offset : offset + limit]
            self._set_rows(rows)
        elif sql_n.startswith("SELECT") and "WHERE id = %s AND created_by_user_id = %s" in sql_n:
            draft_id, owner = params
            row = self._store.get(UUID(draft_id))
            if row and row["created_by_user_id"] == UUID(owner):
                self._set_row(row)
            else:
                self._set_row(None)
        elif sql_n.startswith("UPDATE business.audience_drafts"):
            draft_id = UUID(params[-2])
            owner = UUID(params[-1])
            row = self._store.get(draft_id)
            if row and row["created_by_user_id"] == owner:
                # Parse SET clause to apply updates in order. SQL keeps the
                # column order we passed in; re-derive it from the SQL.
                set_clause = sql_n.split(" SET ", 1)[1].split(" WHERE ")[0]
                cols = [c.split(" = ")[0].strip() for c in set_clause.split(",")]
                values = list(params[: -2])
                for col, val in zip(cols, values, strict=False):
                    if col == "filter_overrides":
                        row[col] = json.loads(val)
                    elif col == "resolved_filters":
                        row[col] = json.loads(val)
                    elif col == "updated_at":
                        # NOW() — handled separately in real SQL; fake updates time
                        continue
                    else:
                        row[col] = val
                row["updated_at"] = datetime.now(tz=__import__("datetime").timezone.utc)
                self._set_row(row)
            else:
                self._set_row(None)
        elif sql_n.startswith("DELETE FROM business.audience_drafts"):
            draft_id, owner = params
            row = self._store.get(UUID(draft_id))
            if row and row["created_by_user_id"] == UUID(owner):
                del self._store[UUID(draft_id)]
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            raise AssertionError(f"unhandled SQL: {sql_n}")

    def _set_row(self, row: dict[str, Any] | None) -> None:
        if row is None:
            self._row = None
            self.description = [(c,) for c in _COLS]
            return
        self.description = [(c,) for c in _COLS]
        self._row = tuple(row[c] for c in _COLS)

    def _set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.description = [(c,) for c in _COLS]
        self._rows = [tuple(r[c] for c in _COLS) for r in rows]

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


_COLS = [
    "id", "created_by_user_id", "name", "audience_template_slug", "source_endpoint",
    "filter_overrides", "resolved_filters",
    "last_preview_total_matched", "last_preview_at",
    "created_at", "updated_at",
]


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

    monkeypatch.setattr(drafts_router, "get_db_connection", fake_get_db)
    return store


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


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


_VALID_BODY = {
    "name": "Texas hazmat carriers — small fleet",
    "audience_template_slug": "motor-carriers-new-entrants-90d",
    "source_endpoint": "/api/v1/fmcsa/audiences/new-entrants-90d",
    "filter_overrides": {"physical_state": ["TX"], "hazmat_flag": True},
    "resolved_filters": {
        "physical_state": ["TX"],
        "hazmat_flag": True,
        "limit": 100,
        "offset": 0,
    },
    "last_preview_total_matched": 412,
    "last_preview_at": "2026-04-29T18:23:11Z",
}


# ────────────────────────────── auth ─────────────────────────────


async def test_create_requires_auth(fake_db) -> None:
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=_VALID_BODY)
    assert resp.status_code == 401


async def test_list_requires_auth(fake_db) -> None:
    async with _client() as c:
        resp = await c.get("/api/audience-drafts")
    assert resp.status_code == 401


# ─────────────────────────── validation ──────────────────────────


async def test_create_rejects_bad_slug(fake_db, as_user_a) -> None:
    body = {**_VALID_BODY, "audience_template_slug": "Bad_Slug!"}
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=body)
    assert resp.status_code == 422


async def test_create_rejects_endpoint_not_under_v1(fake_db, as_user_a) -> None:
    body = {**_VALID_BODY, "source_endpoint": "/elsewhere/foo"}
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=body)
    assert resp.status_code == 422


async def test_create_rejects_array_filter_overrides(fake_db, as_user_a) -> None:
    body = {**_VALID_BODY, "filter_overrides": ["not", "an", "object"]}
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=body)
    assert resp.status_code == 422


async def test_create_rejects_negative_total(fake_db, as_user_a) -> None:
    body = {**_VALID_BODY, "last_preview_total_matched": -1}
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=body)
    assert resp.status_code == 422


async def test_create_rejects_empty_name(fake_db, as_user_a) -> None:
    body = {**_VALID_BODY, "name": ""}
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=body)
    assert resp.status_code == 422


# ────────────────────────── happy-path CRUD ──────────────────────


async def test_full_lifecycle(fake_db, as_user_a) -> None:
    async with _client() as c:
        # Create
        resp = await c.post("/api/audience-drafts", json=_VALID_BODY)
        assert resp.status_code == 201
        created = resp.json()
        draft_id = created["id"]
        assert created["created_by_user_id"] == str(USER_A.auth_user_id)
        assert created["filter_overrides"] == _VALID_BODY["filter_overrides"]
        assert created["resolved_filters"] == _VALID_BODY["resolved_filters"]

        # List
        resp = await c.get("/api/audience-drafts")
        assert resp.status_code == 200
        drafts = resp.json()
        assert len(drafts) == 1
        assert drafts[0]["id"] == draft_id

        # Get
        resp = await c.get(f"/api/audience-drafts/{draft_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == draft_id

        # Update (rename + new overrides)
        new_overrides = {"physical_state": ["CA"]}
        resp = await c.patch(
            f"/api/audience-drafts/{draft_id}",
            json={"name": "California carriers", "filter_overrides": new_overrides},
        )
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["name"] == "California carriers"
        assert updated["filter_overrides"] == new_overrides

        # Delete
        resp = await c.delete(f"/api/audience-drafts/{draft_id}")
        assert resp.status_code == 204

        # Gone
        resp = await c.get(f"/api/audience-drafts/{draft_id}")
        assert resp.status_code == 404


# ─────────────────────── cross-user isolation ────────────────────


async def test_cross_user_404_on_get(fake_db) -> None:
    # User A creates a draft.
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_A
    async with _client() as c:
        resp = await c.post("/api/audience-drafts", json=_VALID_BODY)
        draft_id = resp.json()["id"]
    app.dependency_overrides.clear()

    # User B can't see it.
    app.dependency_overrides[verify_supabase_jwt] = lambda: USER_B
    async with _client() as c:
        resp = await c.get(f"/api/audience-drafts/{draft_id}")
        assert resp.status_code == 404
        # B's list is empty.
        resp = await c.get("/api/audience-drafts")
        assert resp.json() == []
        # B can't update it.
        resp = await c.patch(
            f"/api/audience-drafts/{draft_id}", json={"name": "stolen"}
        )
        assert resp.status_code == 404
        # B can't delete it.
        resp = await c.delete(f"/api/audience-drafts/{draft_id}")
        assert resp.status_code == 404
    app.dependency_overrides.clear()
