"""Unit tests for app.services.initiative_runs_aggregate.

The DB is mocked at the cursor level so these tests assert the
aggregate-shape logic and the fanout expected_count math without
exercising the real DB.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.services import initiative_runs_aggregate as agg
from app.services.gtm_pipeline import PIPELINE_STEPS


INITIATIVE_ID = UUID("11111111-2222-3333-4444-555555555555")


class _FakeCursor:
    def __init__(self, scripted: list[Any]):
        # Shared list — multiple connections in one test pop from the
        # same scripted result queue.
        self._scripted = scripted
        self._next: Any = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, sql, args=()):
        # Pop the scripted result for this query in order.
        self._next = self._scripted.pop(0) if self._scripted else None

    async def fetchall(self):
        return self._next or []

    async def fetchone(self):
        if self._next is None:
            return None
        if isinstance(self._next, list):
            return self._next[0] if self._next else None
        return self._next


class _FakeConn:
    def __init__(self, scripted: list[Any]):
        self._cursor = _FakeCursor(scripted)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def cursor(self):
        return self._cursor


def _patch_db(monkeypatch, scripted: list[Any]):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_conn():
        yield _FakeConn(scripted)

    monkeypatch.setattr(agg, "get_db_connection", fake_conn)


@pytest.mark.asyncio
async def test_aggregate_runs_returns_every_pipeline_slug(monkeypatch):
    # Empty DB → every pipeline slug appears with zero counts.
    _patch_db(monkeypatch, [
        [],   # by-status group-by query
        None, # audience-materializer succeeded row lookup
        None, # channel-step-materializer succeeded row lookup
    ])
    out = await agg.aggregate_runs(INITIATIVE_ID)
    expected_slugs = {pair["actor"] for pair in PIPELINE_STEPS} | {
        pair["verdict"] for pair in PIPELINE_STEPS
    }
    assert set(out.keys()) == expected_slugs
    sd = out["gtm-sequence-definer"]
    assert sd["total_runs"] == 0
    assert sd["fanout"]["is_fanout"] is False
    assert sd["fanout"]["expected_count"] == 0


@pytest.mark.asyncio
async def test_aggregate_runs_fanout_expected_count(monkeypatch):
    # Two DM step ids × 5 recipients → expected_count = 10.
    audience_blob = (
        {"value": {"executed": {"recipient_count": 5, "dub_link_count": 10}}},
    )
    channel_blob = (
        {"value": {"executed": {"dm_step_ids": ["s1", "s2"]}}},
    )
    _patch_db(monkeypatch, [
        [
            ("gtm-per-recipient-creative", "succeeded", 7, 1),
            ("gtm-per-recipient-creative", "failed", 2, 1),
            ("gtm-per-recipient-creative", "superseded", 1, 1),
        ],
        audience_blob,
        channel_blob,
    ])
    out = await agg.aggregate_runs(INITIATIVE_ID)
    fanout = out["gtm-per-recipient-creative"]["fanout"]
    assert fanout["is_fanout"] is True
    assert fanout["expected_count"] == 10  # 5 recipients × 2 DM steps
    assert fanout["completed_count"] == 7
    assert fanout["failed_count"] == 2


def test_dig_returns_default_for_missing_key():
    assert agg._dig({"a": {"b": 1}}, "a", "c", default="X") == "X"
    assert agg._dig({"a": {"b": 1}}, "a", "b") == 1
    assert agg._dig(None, "a") is None
