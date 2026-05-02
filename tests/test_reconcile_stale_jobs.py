"""Tests for app.services.reconciliation.stale_jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.config import settings
from app.services import activation_jobs as jobs_svc
from app.services.reconciliation import stale_jobs as r_stale

JOB_RUNNING = UUID("11111111-1111-1111-1111-111111111111")
JOB_FAILED_OLD = UUID("22222222-2222-2222-2222-222222222222")
JOB_FAILED_RECENT = UUID("33333333-3333-3333-3333-333333333333")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def stub_db_and_transitions(monkeypatch):
    """Stub the DB query + activation_jobs.transition_job."""
    state: dict[str, Any] = {
        "stale_running": [
            (JOB_RUNNING, ORG, "dmaas_campaign_activation",
             datetime.now(UTC) - timedelta(hours=4)),
        ],
        "stale_failed": [
            (JOB_FAILED_OLD, ORG, "dmaas_campaign_activation"),
        ],
        "transitions": [],
        "queries": [],
    }

    class FakeCursor:
        def __init__(self, parent_state: dict[str, Any]) -> None:
            self._state = parent_state
            self._last_query: str = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def execute(self, query: str, args=()) -> None:
            self._last_query = query
            self._state["queries"].append(query)

        async def fetchall(self) -> list[tuple[Any, ...]]:
            if "status = 'running'" in self._last_query:
                return self._state["stale_running"]
            if "status = 'failed'" in self._last_query:
                return self._state["stale_failed"]
            return []

    class FakeConn:
        def __init__(self, parent_state: dict[str, Any]) -> None:
            self._state = parent_state

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor(self._state)

        async def commit(self) -> None:
            return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_get_db_connection():
        async with FakeConn(state) as c:
            yield c

    async def fake_transition(**kwargs):
        state["transitions"].append(kwargs)
        return None  # transition_job returns a job; reconciler ignores

    monkeypatch.setattr(r_stale, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(r_stale.jobs_svc, "transition_job", fake_transition)
    return state


@pytest.mark.asyncio
async def test_reconcile_marks_stale_running_as_failed(
    stub_db_and_transitions, monkeypatch
):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_STALE_JOBS_ENABLED", True)
    result = await r_stale.reconcile()
    assert result.enabled is True
    assert result.rows_scanned >= 1
    assert result.rows_touched >= 1
    failed_transitions = [
        t for t in stub_db_and_transitions["transitions"] if t.get("status") == "failed"
    ]
    assert len(failed_transitions) == 1
    assert failed_transitions[0]["error"]["reason"] == "stale_running_state"


@pytest.mark.asyncio
async def test_reconcile_marks_old_failed_as_dead_lettered(
    stub_db_and_transitions, monkeypatch
):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_STALE_JOBS_ENABLED", True)
    result = await r_stale.reconcile()
    dead_lettered = [
        t for t in stub_db_and_transitions["transitions"]
        if t.get("status") == "dead_lettered"
    ]
    assert len(dead_lettered) == 1
    assert result.drift_found >= 1


@pytest.mark.asyncio
async def test_reconcile_short_circuits_when_disabled(
    stub_db_and_transitions, monkeypatch
):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_STALE_JOBS_ENABLED", False)
    result = await r_stale.reconcile()
    assert result.enabled is False
    assert result.rows_scanned == 0
    assert stub_db_and_transitions["transitions"] == []
