"""Regression tests for app.services.reconciliation.lob_pieces.

The candidate-selection query JOINs business.channel_campaign_steps (s) and
business.channel_campaigns (cc). Both tables expose a ``status`` column, so the
original bare ``status IN (...)`` predicate made Postgres raise AmbiguousColumn
(SQLSTATE 42702) at plan time: the reconciler errored on every tick and never
returned drift (the same reader/schema failure family as the dub_clicks repair).
The literal set was wrong for the step table too — 'sending' is a *campaign*
status (ChannelCampaignStatus) that no step row ever carries, while 'activating'
(the in-flight step state that holds the Lob campaign id) was missing.

The FakeCursor is deliberately SCHEMA-AWARE:
  * it raises (simulating Postgres 42702) if the driving query references a bare,
    unqualified ``status`` — reintroducing the ambiguity fails a test; and
  * it models the seeded step's status against the query's IN-list, so dropping
    'activating' (or reviving the dead 'sending') stops the step being scanned
    and fails a test, not just the column-qualification alone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

import app.providers.lob.client as lob_client_mod
from app.config import settings
from app.services.reconciliation import lob_pieces as r_lob

STEP_ID = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
# Lob's campaign id is an opaque provider string stored verbatim in
# channel_campaign_steps.external_provider_id — never a local UUID PK.
LOB_CAMPAIGN_ID = "cmp_regression"


class FakeAmbiguousColumn(Exception):
    """Stand-in for psycopg.errors.AmbiguousColumn (SQLSTATE 42702)."""


def _guard_schema(query: str) -> None:
    # The driving query joins two tables that both carry a `status` column, so a
    # bare `status IN (...)` is what Postgres rejects with 42702 at plan time. A
    # qualified `s.status` (or `cc.status`) resolves cleanly. channel/provider/
    # external_provider_id each exist on exactly one table, so they are not
    # ambiguous and need no guard here.
    if "FROM business.channel_campaign_steps" in query and "status IN" in query:
        if "s.status IN" not in query and "cc.status IN" not in query:
            raise FakeAmbiguousColumn('column reference "status" is ambiguous')


def _install_fakes(
    monkeypatch,
    *,
    provider_count: int,
    local_count: int,
    step_status: str = "scheduled",
) -> dict[str, Any]:
    """Wire a schema-aware fake DB (one direct_mail/lob step in ``step_status``
    holding a Lob campaign id) plus a fake Lob client reporting ``provider_count``
    pieces. Returns the shared state dict (it captures the executed driving query
    for assertions)."""
    state: dict[str, Any] = {
        # (s.id, s.organization_id, s.external_provider_id) — matches the SELECT
        # and the `for step_id, org_id, lob_campaign_id in steps` unpack.
        "step": (STEP_ID, ORG, LOB_CAMPAIGN_ID),
        "step_status": step_status,
        "local_count": local_count,
        "driving_query": "",
    }

    class FakeCursor:
        def __init__(self, parent_state: dict[str, Any]) -> None:
            self._state = parent_state
            self._query = ""
            self._args: Any = ()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def execute(self, query: str, args=()) -> None:
            _guard_schema(query)  # bare `status` -> 42702, like prod
            self._query = query
            self._args = args
            if "FROM business.channel_campaign_steps" in query:
                self._state["driving_query"] = query

        async def fetchall(self) -> list[tuple[Any, ...]]:
            if "FROM business.channel_campaign_steps" in self._query:
                # Model the WHERE: the seeded step (which always has a non-null
                # external_provider_id) is a candidate only when its status is in
                # the query's IN-list. A query that drops 'activating' or keeps
                # the dead 'sending' won't match an activating step -> not scanned.
                if f"'{self._state['step_status']}'" in self._query:
                    return [self._state["step"]]
                return []
            return []

        async def fetchone(self) -> tuple[Any, ...] | None:
            if "COUNT(*)" in self._query and "direct_mail_pieces" in self._query:
                return (self._state["local_count"],)
            return None

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor(state)

    @asynccontextmanager
    async def fake_get_db_connection():
        yield FakeConn()

    def fake_get_campaign(*, api_key: str, campaign_id: str) -> dict[str, Any]:
        # The reconciler must look Lob up by the step's external_provider_id (the
        # Lob campaign id), never a local PK.
        assert campaign_id == LOB_CAMPAIGN_ID, (
            f"expected Lob campaign id, got {campaign_id!r}"
        )
        return {"piece_count": provider_count}

    monkeypatch.setattr(settings, "DMAAS_RECONCILE_LOB_ENABLED", True)
    monkeypatch.setattr(settings, "LOB_API_KEY", "test-lob-key")
    monkeypatch.setattr(r_lob, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(lob_client_mod, "get_campaign", fake_get_campaign)
    return state


@pytest.mark.asyncio
async def test_no_drift_when_local_matches_provider(monkeypatch):
    # Lob reports 5 pieces; we already hold 5 locally -> no gap. (This also
    # transitively guards the ambiguity fix: a regression to bare `status`
    # raises FakeAmbiguousColumn in execute() before any row is scanned.)
    _install_fakes(monkeypatch, provider_count=5, local_count=5)

    result = await r_lob.reconcile()

    assert result.enabled is True
    assert result.rows_scanned == 1
    assert result.drift_found == 0


@pytest.mark.asyncio
async def test_drift_emitted_when_provider_exceeds_local(monkeypatch):
    # Lob has 10 pieces but only 4 landed locally -> 6-piece gap (dropped webhooks).
    _install_fakes(monkeypatch, provider_count=10, local_count=4)

    result = await r_lob.reconcile()

    assert result.rows_scanned == 1
    assert result.drift_found == 1


@pytest.mark.asyncio
async def test_drift_detail_carries_step_and_org(monkeypatch):
    _install_fakes(monkeypatch, provider_count=4, local_count=1)

    result = await r_lob.reconcile()

    assert result.drift_found == 1
    detail = result.details[0]
    assert detail["kind"] == "missing_pieces"
    assert detail["step_id"] == str(STEP_ID)
    assert detail["organization_id"] == str(ORG)
    assert detail["lob_campaign_id"] == LOB_CAMPAIGN_ID
    assert detail["provider_count"] == 4
    assert detail["local_count"] == 1
    assert detail["gap"] == 3


@pytest.mark.asyncio
async def test_activating_steps_are_scanned(monkeypatch):
    # 'activating' is the in-flight step state that carries the Lob campaign id;
    # a webhook drop mid-activation is exactly the gap this reconciler must catch.
    # The pre-fix filter ('scheduled','sending','sent') omitted 'activating' and
    # silently swallowed that drift. With the corrected step vocabulary the step
    # is scanned and the gap surfaces.
    _install_fakes(
        monkeypatch, provider_count=3, local_count=0, step_status="activating"
    )

    result = await r_lob.reconcile()

    assert result.rows_scanned == 1
    assert result.drift_found == 1


@pytest.mark.asyncio
async def test_candidate_query_is_qualified_and_uses_step_vocabulary(monkeypatch):
    # Pin the whole fix shape so a regression to any single piece fails here,
    # independent of the behavioral tests above.
    state = _install_fakes(monkeypatch, provider_count=1, local_count=1)

    await r_lob.reconcile()

    q = state["driving_query"]
    assert q, "driving query was never executed"
    # `status` is the ambiguous column -> must be qualified to the step table.
    assert "s.status IN" in q
    # Step-status vocabulary: 'activating' in, dead campaign-only 'sending' out.
    assert "'activating'" in q
    assert "'sending'" not in q
    # channel/provider live on the campaign (cc); external_provider_id on the step.
    assert "cc.channel" in q
    assert "cc.provider" in q
    assert "s.external_provider_id" in q


@pytest.mark.asyncio
async def test_short_circuits_when_disabled(monkeypatch):
    _install_fakes(monkeypatch, provider_count=99, local_count=0)
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_LOB_ENABLED", False)

    result = await r_lob.reconcile()

    assert result.enabled is False
    assert result.rows_scanned == 0
    assert result.drift_found == 0


@pytest.mark.asyncio
async def test_short_circuits_when_no_api_key(monkeypatch):
    _install_fakes(monkeypatch, provider_count=99, local_count=0)
    monkeypatch.setattr(settings, "LOB_API_KEY", None)

    result = await r_lob.reconcile()

    assert result.enabled is False
    assert result.rows_scanned == 0
