"""Router-level tests for the Phase-1 GTM slice-to-campaign proxy
endpoints (``/internal/tasks/enrich`` + ``/internal/gtm-slice/*``).

Asserts:
  * Each endpoint is reachable (router registered, mountpoint correct).
  * Each endpoint rejects requests without the TRIGGER_SHARED_SECRET
    bearer (401) and with the wrong bearer (401).
  * Happy-path 200 with the strict Pydantic payload returns the
    structured ack envelope.
  * Strict payload models reject unknown keys (422).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def trigger_headers():
    return {"Authorization": "Bearer test-trigger-secret"}


# ── /internal/tasks/enrich ────────────────────────────────────────────────


def test_tasks_enrich_requires_trigger_secret(client):
    resp = client.post(
        "/internal/tasks/enrich",
        json={
            "run_id": "run_abc",
            "task_type": "firmographic",
            "audience_spec_id": "spec_001",
            "provider_set": ["leadmagic"],
            "inputs_count": 100,
        },
    )
    assert resp.status_code == 401


def test_tasks_enrich_rejects_wrong_bearer(client):
    resp = client.post(
        "/internal/tasks/enrich",
        headers={"Authorization": "Bearer wrong"},
        json={
            "run_id": "run_abc",
            "task_type": "firmographic",
            "audience_spec_id": "spec_001",
        },
    )
    assert resp.status_code == 401


def test_tasks_enrich_happy_path(client, trigger_headers):
    resp = client.post(
        "/internal/tasks/enrich",
        headers=trigger_headers,
        json={
            "run_id": "run_abc",
            "task_type": "firmographic",
            "audience_spec_id": "spec_001",
            "provider_set": ["leadmagic", "parallel"],
            "inputs_count": 250,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["endpoint"] == "tasks.enrich"
    assert body["run_id"] == "run_abc"
    assert body["task_type"] == "firmographic"
    assert body["audience_spec_id"] == "spec_001"


def test_tasks_enrich_rejects_unknown_keys(client, trigger_headers):
    resp = client.post(
        "/internal/tasks/enrich",
        headers=trigger_headers,
        json={
            "run_id": "run_abc",
            "task_type": "firmographic",
            "audience_spec_id": "spec_001",
            "provider_set": [],
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
