"""Pure-logic tests for app.services.activation_jobs.

DB calls are not exercised here — the function-level branches that don't
touch DB (Trigger.dev HTTP API wrapper, error mapping) are covered with
httpx MockTransport.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.config import settings
from app.models.activation_jobs import ActivationJobResponse
from app.services import activation_jobs as jobs_svc

JOB_ID = UUID("99999999-9999-9999-9999-999999999999")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _job(**kw: Any) -> ActivationJobResponse:
    now = datetime.now(UTC)
    base = dict(
        id=JOB_ID,
        organization_id=ORG,
        brand_id=BRAND,
        kind="dmaas_campaign_activation",
        status="queued",
        idempotency_key=None,
        payload={},
        result=None,
        error=None,
        history=[],
        trigger_run_id=None,
        attempts=0,
        created_at=now,
        started_at=None,
        completed_at=None,
        dead_lettered_at=None,
    )
    base.update(kw)
    return ActivationJobResponse(**base)


# ── enqueue_via_trigger ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_via_trigger_returns_run_id(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_API_KEY", "test_key")
    monkeypatch.setattr(settings, "TRIGGER_API_BASE_URL", "https://trigger.example.com")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "run_abc"})

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    run_id = await jobs_svc.enqueue_via_trigger(
        job=_job(), task_identifier="dmaas.process_activation_job"
    )

    assert run_id == "run_abc"
    assert captured["method"] == "POST"
    assert "tasks/dmaas.process_activation_job/trigger" in captured["url"]
    assert captured["auth"] == "Bearer test_key"
    assert str(JOB_ID) in captured["body"]


@pytest.mark.asyncio
async def test_enqueue_via_trigger_propagates_delay(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_API_KEY", "test_key")
    monkeypatch.setattr(settings, "TRIGGER_API_BASE_URL", "https://trigger.example.com")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "run_abc"})

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    await jobs_svc.enqueue_via_trigger(
        job=_job(),
        task_identifier="dmaas.process_activation_job",
        delay_seconds=120,
    )
    assert "delay" in captured["body"]
    assert "120s" in captured["body"]


@pytest.mark.asyncio
async def test_enqueue_via_trigger_raises_on_4xx(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_API_KEY", "test_key")
    monkeypatch.setattr(settings, "TRIGGER_API_BASE_URL", "https://trigger.example.com")

    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, text="bad token")
    )
    real_client_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    with pytest.raises(jobs_svc.TriggerEnqueueError) as exc_info:
        await jobs_svc.enqueue_via_trigger(
            job=_job(), task_identifier="dmaas.process_activation_job"
        )
    assert "401" in str(exc_info.value)


@pytest.mark.asyncio
async def test_enqueue_via_trigger_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_API_KEY", None)
    with pytest.raises(jobs_svc.TriggerEnqueueError):
        await jobs_svc.enqueue_via_trigger(
            job=_job(), task_identifier="dmaas.process_activation_job"
        )


@pytest.mark.asyncio
async def test_cancel_trigger_run_calls_v2_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_API_KEY", "test_key")
    monkeypatch.setattr(settings, "TRIGGER_API_BASE_URL", "https://trigger.example.com")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"id": "run_abc"})

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    await jobs_svc.cancel_trigger_run("run_abc")
    assert captured["method"] == "POST"
    assert "/api/v2/runs/run_abc/cancel" in captured["url"]
