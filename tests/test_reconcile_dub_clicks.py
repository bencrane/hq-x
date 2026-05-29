"""Regression tests for app.services.reconciliation.dub_clicks.

Guards the predicate-value contract between the writer/schema and the
reconciliation reader. Click rows land in ``dmaas_dub_events`` as
``event_type='link.clicked'`` — that is the Dub wire event name the
webhook processor inserts verbatim, and the migration 0020 CHECK
constraint only admits ``('link.clicked','lead.created','sale.created')``.

The historical bug counted local clicks with ``event_type = 'click'`` —
a value the CHECK constraint forbids — so ``local_clicks`` was always 0
and every active link with real clicks emitted a false
``dub_click_drift`` event on every cron tick. ``test_no_drift_*`` below
fails against that buggy predicate and passes once it reads
``'link.clicked'``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

import app.providers.dub.client as dub_client_mod
from app.config import settings
from app.services.reconciliation import dub_clicks as r_dub

LINK = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DUB_LINK_ID = "link_regression"


def _install_fakes(
    monkeypatch, *, events: list[str], provider_clicks: int
) -> None:
    """Wire a fake DB (one link + the given event rows) and a fake Dub client.

    ``events`` is the list of ``event_type`` values seeded for the single
    link. The fake COUNT only tallies a seeded row when the executed query
    actually filters on that row's ``event_type`` literal — so the count is
    sensitive to the exact predicate value, which is the whole point of the
    regression.
    """
    state: dict[str, Any] = {
        "links": [(LINK, ORG, DUB_LINK_ID)],
        "events": [{"link": str(LINK), "event_type": et} for et in events],
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
            self._query = query
            self._args = args

        async def fetchall(self) -> list[tuple[Any, ...]]:
            if "FROM dmaas_dub_links" in self._query:
                return self._state["links"]
            return []

        async def fetchone(self) -> tuple[Any, ...] | None:
            if "COUNT(*)" in self._query and "dmaas_dub_events" in self._query:
                link = self._args[0] if self._args else None
                # Tally only rows whose event_type matches the literal the
                # query actually filters on. The buggy 'click' predicate
                # matches no seeded row (count 0 -> false drift); the correct
                # 'link.clicked' predicate matches the seeded click rows.
                n = sum(
                    1
                    for e in self._state["events"]
                    if e["link"] == link
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
        assert link_id == DUB_LINK_ID
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
    # The bug filtered on 'click' -> counted 0 local clicks -> false drift.
    # The fix counts 3 -> provider (3) == local (3) -> no drift.
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
    # A reader that counted *all* event rows would see local (4) >= 2 and
    # wrongly suppress the drift, so this pins the predicate to clicks only.
    _install_fakes(
        monkeypatch,
        events=["link.clicked", "lead.created", "sale.created", "lead.created"],
        provider_clicks=2,
    )

    result = await r_dub.reconcile()

    assert result.drift_found == 1


@pytest.mark.asyncio
async def test_short_circuits_when_disabled(monkeypatch):
    _install_fakes(monkeypatch, events=["link.clicked"] * 3, provider_clicks=99)
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_DUB_ENABLED", False)

    result = await r_dub.reconcile()

    assert result.enabled is False
    assert result.rows_scanned == 0
    assert result.drift_found == 0
