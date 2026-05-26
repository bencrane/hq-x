"""Router-level tests for the Phase-1 GTM slice-to-campaign proxy
endpoints (``/internal/tasks/enrich`` + ``/internal/gtm-slice/*``).

Asserts:
  * Each endpoint is reachable (router registered, mountpoint correct).
  * Each endpoint rejects requests without the TRIGGER_SHARED_SECRET
    bearer (401) and with the wrong bearer (401).
  * Happy-path 200 with the strict Pydantic payload returns the
    structured ack envelope.
  * Strict payload models reject unknown keys (422).
  * ``/internal/tasks/enrich`` writes a row into ``ops.task_runs`` and
    surfaces a 500 when the ledger insert fails.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.internal import gtm_pipeline as gtm_pipeline_router


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def trigger_headers():
    return {"Authorization": "Bearer test-trigger-secret"}


# ── Fake DB plumbing for the ledger insert ────────────────────────────────


class _FakeCursor:
    def __init__(self, capture: list[dict[str, Any]]):
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._capture.append({"sql": sql, "params": params})


class _FakeConn:
    def __init__(self, capture: list[dict[str, Any]]):
        self._capture = capture
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._capture)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def db_capture(monkeypatch):
    capture: list[dict[str, Any]] = []
    conn = _FakeConn(capture)

    @asynccontextmanager
    async def _conn():
        yield conn

    monkeypatch.setattr(gtm_pipeline_router, "get_db_connection", _conn)
    return {"capture": capture, "conn": conn}


@pytest.fixture
def db_raises(monkeypatch):
    @asynccontextmanager
    async def _conn():
        raise RuntimeError("db down")
        yield  # pragma: no cover — unreachable, here to satisfy the generator shape

    monkeypatch.setattr(gtm_pipeline_router, "get_db_connection", _conn)


# ── /internal/tasks/enrich ────────────────────────────────────────────────


_ENTITY = {
    "id": "ent_0001",
    "domain": "acme-trucking.com",
    "linkedin_url": "https://www.linkedin.com/company/acme-trucking",
}


def test_tasks_enrich_requires_trigger_secret(client):
    resp = client.post(
        "/internal/tasks/enrich",
        json={
            "task_run_id": "run_abc",
            "provider": "blitz",
            "action": "find_work_email",
            "entity_data": _ENTITY,
        },
    )
    assert resp.status_code == 401


def test_tasks_enrich_rejects_wrong_bearer(client):
    resp = client.post(
        "/internal/tasks/enrich",
        headers={"Authorization": "Bearer wrong"},
        json={
            "task_run_id": "run_abc",
            "provider": "blitz",
            "action": "find_work_email",
            "entity_data": _ENTITY,
        },
    )
    assert resp.status_code == 401


def test_tasks_enrich_happy_path(client, trigger_headers, db_capture):
    resp = client.post(
        "/internal/tasks/enrich",
        headers=trigger_headers,
        json={
            "task_run_id": "run_abc",
            "provider": "blitz",
            "action": "find_work_email",
            "entity_data": _ENTITY,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["endpoint"] == "tasks.enrich"
    assert body["task_run_id"] == "run_abc"
    assert body["task_type"] == "blitz_find_work_email"

    capture = db_capture["capture"]
    assert len(capture) == 1
    assert "INSERT INTO ops.task_runs" in capture[0]["sql"]
    assert capture[0]["params"] == (
        "run_abc",
        "blitz_find_work_email",
        "pending",
        1,
    )
    assert db_capture["conn"].commits == 1


def test_tasks_enrich_returns_500_on_ledger_failure(
    client, trigger_headers, db_raises
):
    resp = client.post(
        "/internal/tasks/enrich",
        headers=trigger_headers,
        json={
            "task_run_id": "run_abc",
            "provider": "blitz",
            "action": "find_work_email",
            "entity_data": _ENTITY,
        },
    )
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "ledger_insert_failed"


def test_tasks_enrich_rejects_unknown_keys(client, trigger_headers):
    resp = client.post(
        "/internal/tasks/enrich",
        headers=trigger_headers,
        json={
            "task_run_id": "run_abc",
            "provider": "blitz",
            "action": "find_work_email",
            "entity_data": _ENTITY,
            "rogue_key": "nope",
        },
    )
    assert resp.status_code == 422


# ── /internal/gtm-slice/resolve ───────────────────────────────────────────


def test_gtm_slice_resolve_requires_trigger_secret(client):
    resp = client.post(
        "/internal/gtm-slice/resolve",
        json={"pipeline_run_id": "pipe_1", "audience_spec_id": "spec_001"},
    )
    assert resp.status_code == 401


def test_gtm_slice_resolve_happy_path(client, trigger_headers):
    resp = client.post(
        "/internal/gtm-slice/resolve",
        headers=trigger_headers,
        json={"pipeline_run_id": "pipe_1", "audience_spec_id": "spec_001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["endpoint"] == "gtm-slice.resolve"
    assert body["pipeline_run_id"] == "pipe_1"
    assert body["audience_spec_id"] == "spec_001"


# ── /internal/gtm-slice/find-people ───────────────────────────────────────


def test_gtm_slice_find_people_requires_trigger_secret(client):
    resp = client.post(
        "/internal/gtm-slice/find-people",
        json={
            "pipeline_run_id": "pipe_1",
            "audience_spec_id": "spec_001",
            "provider_set": ["leadmagic"],
        },
    )
    assert resp.status_code == 401


def test_gtm_slice_find_people_happy_path(client, trigger_headers):
    resp = client.post(
        "/internal/gtm-slice/find-people",
        headers=trigger_headers,
        json={
            "pipeline_run_id": "pipe_1",
            "audience_spec_id": "spec_001",
            "provider_set": ["leadmagic", "parallel"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["endpoint"] == "gtm-slice.find-people"


# ── /internal/gtm-slice/validate ──────────────────────────────────────────


def test_gtm_slice_validate_requires_trigger_secret(client):
    resp = client.post(
        "/internal/gtm-slice/validate",
        json={
            "pipeline_run_id": "pipe_1",
            "audience_spec_id": "spec_001",
            "provider_set": ["millionverifier"],
        },
    )
    assert resp.status_code == 401


def test_gtm_slice_validate_happy_path(client, trigger_headers):
    resp = client.post(
        "/internal/gtm-slice/validate",
        headers=trigger_headers,
        json={
            "pipeline_run_id": "pipe_1",
            "audience_spec_id": "spec_001",
            "provider_set": ["millionverifier"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["endpoint"] == "gtm-slice.validate"


def test_gtm_slice_validate_rejects_unknown_keys(client, trigger_headers):
    resp = client.post(
        "/internal/gtm-slice/validate",
        headers=trigger_headers,
        json={
            "pipeline_run_id": "pipe_1",
            "audience_spec_id": "spec_001",
            "provider_set": [],
            "extra_field": "nope",
        },
    )
    assert resp.status_code == 422
