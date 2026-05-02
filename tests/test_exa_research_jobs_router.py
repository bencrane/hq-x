"""Tests for /api/v1/exa/jobs (public) + /internal/exa/jobs/{id}/process.

We stub out:
  * exa_research_jobs service (in-memory job store)
  * activation_jobs.enqueue_via_trigger (so no real Trigger.dev call)
  * exa_client (so no real Exa call)
  * exa_call_persistence (so no real DB / no real DEX call)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.routers import exa_jobs as public_router_mod
from app.routers.internal import exa_jobs as internal_router_mod
from app.services import activation_jobs as activation_jobs_svc
from app.services import exa_call_persistence
from app.services import exa_client
from app.services import exa_research_jobs as exa_jobs_svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_OTHER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
USER = UUID("11111111-1111-1111-1111-111111111111")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER,
        business_user_id=USER,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_no_org():
    user = _user(None)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    yield
    app.dependency_overrides.clear()


def _job_row(
    *,
    id_: UUID | None = None,
    organization_id: UUID = ORG,
    status: str = "queued",
    trigger_run_id: str | None = None,
    destination: str = "hqx",
    endpoint: str = "search",
    objective: str = "demo",
    request_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": id_ or uuid4(),
        "organization_id": organization_id,
        "created_by_user_id": USER,
        "endpoint": endpoint,
        "destination": destination,
        "objective": objective,
        "objective_ref": None,
        "request_payload": request_payload or {"query": "hello"},
        "status": status,
        "result_ref": None,
        "error": None,
        "history": [],
        "trigger_run_id": trigger_run_id,
        "idempotency_key": idempotency_key,
        "attempts": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }


@pytest.fixture
def stub_jobs(monkeypatch):
    state: dict[str, Any] = {
        "jobs_by_id": {},
        "jobs_by_idem": {},
        "create_calls": [],
        "enqueue_calls": [],
        "transitions": [],
        "enqueue_should_raise": False,
        "current_job": None,
    }

    async def fake_create(**kwargs):
        state["create_calls"].append(kwargs)
        idem = kwargs.get("idempotency_key")
        if idem:
            existing_id = state["jobs_by_idem"].get((kwargs["organization_id"], idem))
            if existing_id is not None:
                return state["jobs_by_id"][existing_id]
        job = _job_row(
            organization_id=kwargs["organization_id"],
            destination=kwargs["destination"],
            endpoint=kwargs["endpoint"],
            objective=kwargs["objective"],
            request_payload=kwargs.get("request_payload") or {},
            idempotency_key=idem,
        )
        state["jobs_by_id"][job["id"]] = job
        if idem:
            state["jobs_by_idem"][(kwargs["organization_id"], idem)] = job["id"]
        state["current_job"] = job
        return job

    async def fake_get(job_id, *, organization_id=None):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is None:
            return None
        if organization_id is not None and job["organization_id"] != organization_id:
            return None
        return job

    async def fake_update_run_id(job_id, run_id):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is not None:
            job["trigger_run_id"] = run_id

    async def fake_mark_running(job_id, run_id):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is not None:
            job["status"] = "running"
            job["trigger_run_id"] = run_id or job["trigger_run_id"]
        state["transitions"].append(("running", str(job_id), run_id))

    async def fake_mark_succeeded(job_id, result_ref):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is not None:
            job["status"] = "succeeded"
            job["result_ref"] = result_ref
        state["transitions"].append(("succeeded", str(job_id), result_ref))

    async def fake_mark_failed(job_id, error):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is not None:
            job["status"] = "failed"
            job["error"] = error
        state["transitions"].append(("failed", str(job_id), error))

    async def fake_append_history(job_id, event):
        job = state["jobs_by_id"].get(UUID(str(job_id)))
        if job is not None:
            job["history"].append(event)

    monkeypatch.setattr(exa_jobs_svc, "create_job", fake_create)
    monkeypatch.setattr(exa_jobs_svc, "get_job", fake_get)
    monkeypatch.setattr(exa_jobs_svc, "update_trigger_run_id", fake_update_run_id)
    monkeypatch.setattr(exa_jobs_svc, "mark_running", fake_mark_running)
    monkeypatch.setattr(exa_jobs_svc, "mark_succeeded", fake_mark_succeeded)
    monkeypatch.setattr(exa_jobs_svc, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(exa_jobs_svc, "append_history", fake_append_history)
    monkeypatch.setattr(public_router_mod.exa_jobs_svc, "create_job", fake_create)
    monkeypatch.setattr(public_router_mod.exa_jobs_svc, "get_job", fake_get)
    monkeypatch.setattr(
        public_router_mod.exa_jobs_svc, "update_trigger_run_id", fake_update_run_id
    )
    monkeypatch.setattr(public_router_mod.exa_jobs_svc, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(internal_router_mod.exa_jobs_svc, "get_job", fake_get)
    monkeypatch.setattr(internal_router_mod.exa_jobs_svc, "mark_running", fake_mark_running)
    monkeypatch.setattr(
        internal_router_mod.exa_jobs_svc, "mark_succeeded", fake_mark_succeeded
    )
    monkeypatch.setattr(internal_router_mod.exa_jobs_svc, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(
        internal_router_mod.exa_jobs_svc, "append_history", fake_append_history
    )

    async def fake_enqueue(*, task_identifier, payload_override=None, **kwargs):
        state["enqueue_calls"].append(
            {"task_identifier": task_identifier, "payload": payload_override}
        )
        if state["enqueue_should_raise"]:
            raise activation_jobs_svc.TriggerEnqueueError("trigger.dev down")
        return "run_test_xyz"

    monkeypatch.setattr(activation_jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(
        public_router_mod.jobs_svc, "enqueue_via_trigger", fake_enqueue
    )
    return state


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.request(method, path, **kwargs)


# ── Public POST ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_returns_202_and_enqueues_task(auth_org_a, stub_jobs):
    body = {
        "endpoint": "search",
        "destination": "hqx",
        "objective": "customer_research",
        "request_payload": {"query": "DAT competitors", "num_results": 3},
    }
    resp = await _request("POST", "/api/v1/exa/jobs", json=body)
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert "job_id" in payload
    assert payload["status"] == "queued"
    assert len(stub_jobs["create_calls"]) == 1
    assert len(stub_jobs["enqueue_calls"]) == 1
    enq = stub_jobs["enqueue_calls"][0]
    assert enq["task_identifier"] == "exa.process_research_job"
    assert enq["payload"] == {"job_id": payload["job_id"]}


@pytest.mark.asyncio
async def test_create_job_idempotent_returns_existing(auth_org_a, stub_jobs):
    body = {
        "endpoint": "search",
        "destination": "hqx",
        "objective": "customer_research",
        "request_payload": {"query": "x"},
        "idempotency_key": "dedupe-1",
    }
    resp_a = await _request("POST", "/api/v1/exa/jobs", json=body)
    # Mark the job as already trigger-enqueued so the second call short-circuits.
    job_id = resp_a.json()["job_id"]
    stub_jobs["jobs_by_id"][UUID(job_id)]["trigger_run_id"] = "run_test_xyz"

    resp_b = await _request("POST", "/api/v1/exa/jobs", json=body)

    assert resp_a.json()["job_id"] == resp_b.json()["job_id"]
    # Only the first call should have hit Trigger.
    assert len(stub_jobs["enqueue_calls"]) == 1


@pytest.mark.asyncio
async def test_create_job_no_org_returns_400(auth_no_org, stub_jobs):
    body = {
        "endpoint": "search",
        "destination": "hqx",
        "objective": "demo",
        "request_payload": {"query": "x"},
    }
    resp = await _request("POST", "/api/v1/exa/jobs", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


@pytest.mark.asyncio
async def test_get_job_cross_org_returns_404(auth_org_a, stub_jobs):
    # Insert a job owned by another org.
    foreign = _job_row(organization_id=ORG_OTHER)
    stub_jobs["jobs_by_id"][foreign["id"]] = foreign
    resp = await _request("GET", f"/api/v1/exa/jobs/{foreign['id']}")
    assert resp.status_code == 404


# ── Internal /process ────────────────────────────────────────────────────


@pytest.fixture
def auth_trigger():
    return {"Authorization": "Bearer test-trigger-secret"}


@pytest.mark.asyncio
async def test_internal_process_dispatches_and_persists_local(
    auth_trigger, stub_jobs, monkeypatch
):
    # Arrange a job in the in-memory store.
    job = _job_row(destination="hqx", endpoint="search")
    stub_jobs["jobs_by_id"][job["id"]] = job

    async def fake_search(**kwargs):
        return {
            "results": [{"title": "Acme"}],
            "_meta": {
                "duration_ms": 42,
                "exa_request_id": "req_x",
                "cost_dollars": 0.01,
            },
        }

    monkeypatch.setattr(exa_client, "search", fake_search)
    monkeypatch.setitem(internal_router_mod._ENDPOINT_DISPATCH, "search", fake_search)

    captured: dict[str, Any] = {}

    async def fake_persist_local(**kwargs):
        captured["local"] = kwargs
        return UUID("11112222-3333-4444-5555-666677778888")

    async def fake_persist_dex(**kwargs):  # pragma: no cover
        raise AssertionError("dex path should not run for hqx destination")

    monkeypatch.setattr(
        exa_call_persistence, "persist_exa_call_local", fake_persist_local
    )
    monkeypatch.setattr(exa_call_persistence, "persist_exa_call_to_dex", fake_persist_dex)

    resp = await _request(
        "POST",
        f"/internal/exa/jobs/{job['id']}/process",
        json={"trigger_run_id": "run_a"},
        headers=auth_trigger,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result_ref"] == (
        "hqx://exa.exa_calls/11112222-3333-4444-5555-666677778888"
    )
    assert captured["local"]["status"] == "succeeded"
    assert captured["local"]["exa_request_id"] == "req_x"
    assert captured["local"]["duration_ms"] == 42


@pytest.mark.asyncio
async def test_internal_process_dispatches_and_persists_to_dex(
    auth_trigger, stub_jobs, monkeypatch
):
    job = _job_row(destination="dex", endpoint="search")
    stub_jobs["jobs_by_id"][job["id"]] = job

    async def fake_search(**kwargs):
        return {
            "results": [],
            "_meta": {"duration_ms": 5, "exa_request_id": None, "cost_dollars": None},
        }

    monkeypatch.setattr(exa_client, "search", fake_search)
    monkeypatch.setitem(internal_router_mod._ENDPOINT_DISPATCH, "search", fake_search)

    captured: dict[str, Any] = {}

    async def fake_persist_local(**kwargs):  # pragma: no cover
        raise AssertionError("local path should not run for dex destination")

    async def fake_persist_dex(**kwargs):
        captured["dex"] = kwargs
        return UUID("aaaa1111-bbbb-2222-cccc-333344445555")

    monkeypatch.setattr(
        exa_call_persistence, "persist_exa_call_local", fake_persist_local
    )
    monkeypatch.setattr(exa_call_persistence, "persist_exa_call_to_dex", fake_persist_dex)

    resp = await _request(
        "POST",
        f"/internal/exa/jobs/{job['id']}/process",
        json={"trigger_run_id": "run_b"},
        headers=auth_trigger,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result_ref"].startswith("dex://exa.exa_calls/")


@pytest.mark.asyncio
async def test_internal_process_marks_failed_on_exa_error(
    auth_trigger, stub_jobs, monkeypatch
):
    job = _job_row(destination="hqx", endpoint="search")
    stub_jobs["jobs_by_id"][job["id"]] = job

    async def fake_search(**kwargs):
        raise exa_client.ExaCallError(
            status_code=500, body="boom", endpoint="search"
        )

    monkeypatch.setattr(exa_client, "search", fake_search)
    monkeypatch.setitem(internal_router_mod._ENDPOINT_DISPATCH, "search", fake_search)

    captured: dict[str, Any] = {}

    async def fake_persist_local(**kwargs):
        captured.update(kwargs)
        return UUID("11112222-3333-4444-5555-666677778888")

    monkeypatch.setattr(
        exa_call_persistence, "persist_exa_call_local", fake_persist_local
    )

    resp = await _request(
        "POST",
        f"/internal/exa/jobs/{job['id']}/process",
        json={"trigger_run_id": "run_c"},
        headers=auth_trigger,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert captured["status"] == "failed"
    # Local job row was transitioned to failed.
    assert stub_jobs["jobs_by_id"][job["id"]]["status"] == "failed"


@pytest.mark.asyncio
async def test_internal_process_idempotent_on_terminal_job(
    auth_trigger, stub_jobs, monkeypatch
):
    job = _job_row(destination="hqx", status="succeeded")
    stub_jobs["jobs_by_id"][job["id"]] = job

    # If dispatch ran we'd blow up; assert no Exa call happens.
    async def fake_search(**kwargs):  # pragma: no cover
        raise AssertionError("should not dispatch on terminal job")

    monkeypatch.setattr(exa_client, "search", fake_search)
    monkeypatch.setitem(internal_router_mod._ENDPOINT_DISPATCH, "search", fake_search)

    resp = await _request(
        "POST",
        f"/internal/exa/jobs/{job['id']}/process",
        json={},
        headers=auth_trigger,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] is True
    assert body["reason"] == "job_already_terminal"
