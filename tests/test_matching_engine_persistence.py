"""matching_engine.persistence.transition_match guard + idempotency tests.

Covers the documented cron-re-fire bug — day-2 evaluate_relationship_for_intent
sees an upserted match still at 'surfaced' and called transition_match(id,
'surfaced') unconditionally, which used to raise InvalidTransition and abort
the relationship iteration. The fix makes identity transitions a silent no-op
while preserving the rest of the state-machine guard.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from app.services.matching_engine import persistence as persistence_mod
from app.services.matching_engine.persistence import (
    InvalidTransition,
    transition_match,
)

_MATCH_ID = UUID("11111111-1111-1111-1111-111111111111")


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
        return self._current


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

    monkeypatch.setattr(persistence_mod, "get_db_connection", _conn)
    return capture


@pytest.mark.asyncio
async def test_surfaced_to_surfaced_is_idempotent_noop(monkeypatch):
    """The day-2 cron bug: re-firing finds the match already at 'surfaced'
    and calls transition_match(id, 'surfaced'). Must not raise, must not
    issue an UPDATE."""
    capture = _patch_db(monkeypatch, [("surfaced",)])

    await transition_match(_MATCH_ID, "surfaced")

    # Only the SELECT should have executed — no UPDATE.
    assert len(capture) == 1
    assert "SELECT status" in capture[0]["sql"]


@pytest.mark.asyncio
async def test_identified_to_surfaced_updates(monkeypatch):
    """Forward transition still works and issues the UPDATE."""
    capture = _patch_db(monkeypatch, [("identified",), None])

    await transition_match(_MATCH_ID, "surfaced")

    assert len(capture) == 2
    assert "SELECT status" in capture[0]["sql"]
    assert "UPDATE business.matches" in capture[1]["sql"]
    assert capture[1]["params"] == ("surfaced", str(_MATCH_ID))


@pytest.mark.asyncio
async def test_terminal_self_transition_is_noop(monkeypatch):
    """claimed → claimed: terminal-state-to-itself short-circuits cleanly
    without consulting the allowed-set (which is empty for terminals)."""
    capture = _patch_db(monkeypatch, [("claimed",)])

    await transition_match(_MATCH_ID, "claimed")

    assert len(capture) == 1
    assert "SELECT status" in capture[0]["sql"]


@pytest.mark.asyncio
async def test_skipping_state_still_raises(monkeypatch):
    """identified → claimed (skipping surfaced/viewed/reserved) is not
    allowed by the graph and must still raise."""
    _patch_db(monkeypatch, [("identified",)])

    with pytest.raises(InvalidTransition, match="from 'identified' to 'claimed'"):
        await transition_match(_MATCH_ID, "claimed")


@pytest.mark.asyncio
async def test_terminal_to_other_still_raises(monkeypatch):
    """claimed → dismissed: terminal state to a non-self target is still
    disallowed (terminal means terminal)."""
    _patch_db(monkeypatch, [("claimed",)])

    with pytest.raises(InvalidTransition, match="from 'claimed' to 'dismissed'"):
        await transition_match(_MATCH_ID, "dismissed")


@pytest.mark.asyncio
async def test_missing_match_raises(monkeypatch):
    """SELECT returns nothing → raise (caller is asking about a row that
    doesn't exist)."""
    _patch_db(monkeypatch, [None])

    with pytest.raises(InvalidTransition, match="not found"):
        await transition_match(_MATCH_ID, "surfaced")
