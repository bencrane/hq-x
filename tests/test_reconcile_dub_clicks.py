"""Regression tests for app.services.reconciliation.dub_clicks.

This reconciler had a family of reader/schema mismatches: it filtered click
rows on event_type='click' (a value migration 0020's CHECK constraint forbids;
click rows are stored as 'link.clicked'), counted them against a non-existent
column dmaas_dub_events.dmaas_dub_link_id (the real column is dub_link_id), and
its driving query selected dl.organization_id / dl.link_id off dmaas_dub_links
(which has neither — org lives on business.brands via brand_id; the link id
column is dub_link_id). Against the real schema the driving query raised
UndefinedColumn every tick, so the cron never ran.

These tests pin the whole contract. The FakeCursor is deliberately
SCHEMA-AWARE: it raises (simulating Postgres UndefinedColumn) if a query
references any of the phantom columns, and it keys click rows by the Dub
``dub_link_id`` string — so reintroducing any of the column bugs, the join-value
bug (passing the PK), or the event_type literal bug fails a test, not just the
event_type literal alone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

import app.providers.dub.client as dub_client_mod
from app.config import settings
from app.services.reconciliation import dub_clicks as r_dub

# dmaas_dub_events.dub_link_id / dmaas_dub_links.dub_link_id store Dub's
# 'link_…' string, NOT a local UUID PK.
DUB_LINK_ID = "link_regression"
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

# Columns the reconciler historically referenced that do not exist in the
# schema (migrations 0019/0020 + canonical app/dmaas/dub_links.py). Touching any
# of them must surface as a hard failure, the way real Postgres would.
_PHANTOM_COLUMNS = ("dmaas_dub_link_id", "dl.organization_id", "dl.link_id")


class FakeUndefinedColumn(Exception):
    """Stand-in for psycopg.errors.UndefinedColumn."""


def _guard_schema(query: str) -> None:
    for col in _PHANTOM_COLUMNS:
        if col in query:
            raise FakeUndefinedColumn(f'column "{col}" does not exist')


def _install_fakes(monkeypatch, *, events: list[str], provider_clicks: int) -> None:
    """Wire a schema-aware fake DB (one link + the given click rows) and a fake
    Dub client. ``events`` is the list of event_type values stored for the link.
    """
    state: dict[str, Any] = {
        "links": [(DUB_LINK_ID, ORG)],
        "events": [{"dub_link_id": DUB_LINK_ID, "event_type": et} for et in events],
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
            _guard_schema(query)  # phantom column -> UndefinedColumn, like prod
            self._query = query
            self._args = args

        async def fetchall(self) -> list[tuple[Any, ...]]:
            if "FROM dmaas_dub_links" in self._query:
                return self._state["links"]
            return []

        async def fetchone(self) -> tuple[Any, ...] | None:
            if "COUNT(*)" in self._query and "dmaas_dub_events" in self._query:
                bound = self._args[0] if self._args else None
                # Tally only rows whose event_type matches the literal the query
                # filters on AND whose dub_link_id matches the bound value. The
                # buggy 'click' predicate matches no row; passing the PK instead
                # of the Dub string matches no row either.
                n = sum(
                    1
                    for e in self._state["events"]
                    if e["dub_link_id"] == bound
                    and f"event_type = '{e['event_type']}'" in self._query
                )
                return (n,)
            return None

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor(state)

    @asynccontextmanager
    async def fake_get_db_connection():
        yield FakeConn()

    def fake_get_link(*, api_key: str, link_id: str, base_url: str | None = None):
        # The cron must look Dub up by the Dub link string (dl.dub_link_id),
        # never the local PK.
        assert link_id == DUB_LINK_ID, f"expected Dub link id, got {link_id!r}"
        return {"clicks": provider_clicks}

    monkeypatch.setattr(settings, "DMAAS_RECONCILE_DUB_ENABLED", True)
    monkeypatch.setattr(r_dub, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(dub_client_mod, "get_link", fake_get_link)


@pytest.mark.asyncio
async def test_no_drift_when_local_link_clicked_count_matches_provider(monkeypatch):
    # 3 real clicks recorded locally as 'link.clicked'; Dub also reports 3.
    _install_fakes(monkeypatch, events=["link.clicked"] * 3, provider_clicks=3)

    result = await r_dub.reconcile()

    assert result.enabled is True
    assert result.rows_scanned == 1
    # The bug filtered on 'click' -> counted 0 -> false drift. The fix counts 3
    # -> provider (3) == local (3) -> no drift.
    assert result.drift_found == 0


@pytest.mark.asyncio
async def test_drift_emitted_when_provider_exceeds_local(monkeypatch):
    # Only 2 clicks landed locally but Dub saw 5 -> genuine 3-click gap.
    _install_fakes(monkeypatch, events=["link.clicked"] * 2, provider_clicks=5)

    result = await r_dub.reconcile()

    assert result.rows_scanned == 1
    assert result.drift_found == 1


@pytest.mark.asyncio
async def test_only_link_clicked_rows_count_not_other_event_types(monkeypatch):
    # 1 click plus lead/sale rows for the same link; Dub reports 2 clicks.
    # Counting only 'link.clicked' -> local (1) < provider (2) -> drift.
    # A reader counting *all* event rows would see local (4) >= 2 and wrongly
    # suppress the drift, so this pins the predicate to clicks only.
    _install_fakes(
        monkeypatch,
        events=["link.clicked", "lead.created", "sale.created", "lead.created"],
        provider_clicks=2,
    )

    result = await r_dub.reconcile()

    assert result.drift_found == 1


@pytest.mark.asyncio
async def test_drift_detail_carries_dub_link_id_and_org(monkeypatch):
    # Org is derived via dmaas_dub_links.brand_id -> business.brands; the drift
    # detail must carry the Dub link string and the resolved org id.
    _install_fakes(monkeypatch, events=["link.clicked"], provider_clicks=4)

    result = await r_dub.reconcile()

    assert result.drift_found == 1
    detail = result.details[0]
    assert detail["kind"] == "dub_click_drift"
    assert detail["dub_link_id"] == DUB_LINK_ID
    assert detail["organization_id"] == str(ORG)
    assert detail["local_clicks"] == 1
    assert detail["provider_clicks"] == 4
    assert detail["gap"] == 3


@pytest.mark.asyncio
async def test_short_circuits_when_disabled(monkeypatch):
    _install_fakes(monkeypatch, events=["link.clicked"] * 3, provider_clicks=99)
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_DUB_ENABLED", False)

    result = await r_dub.reconcile()

    assert result.enabled is False
    assert result.rows_scanned == 0
    assert result.drift_found == 0
