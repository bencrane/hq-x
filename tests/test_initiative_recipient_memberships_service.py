"""Service-layer tests for app.services.initiative_recipient_memberships
against an in-memory DB fake.

The fake intercepts ``get_db_connection`` and dispatches the small set
of queries the service emits. Validates:

  * ``add_membership`` — happy path insert + idempotent re-insert
    (returns the same active row, doesn't duplicate).
  * ``remove_membership`` — soft-delete + re-add after removal works.
  * ``find_active_for_recipient`` — only returns ``removed_at IS NULL``.
  * ``find_active_by_audience_spec`` — same filter, used for
    overlap-detection.
  * ``list_active_for_contract`` — same filter, billing reporting.
  * ``count_active_for_initiative`` — counts only active rows.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import initiative_recipient_memberships as irm_service
from app.services.initiative_recipient_memberships import (
    add_membership,
    count_active_for_initiative,
    find_active_by_audience_spec,
    find_active_for_recipient,
    list_active_for_contract,
    remove_membership,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


@dataclass
class _Store:
    rows: dict[UUID, dict[str, Any]] = field(default_factory=dict)


class _FakeCursor:
    def __init__(self, store: _Store) -> None:
        self._s = store
        self._row: tuple | None = None
        self._rows: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def _row_tuple(self, m: dict[str, Any]) -> tuple:
        return (
            m["id"], m["initiative_id"], m["partner_contract_id"],
            m["recipient_id"], m["data_engine_audience_id"],
            m["first_seen_channel_campaign_id"],
            m["added_at"], m["removed_at"], m["removed_reason"],
        )

    async def execute(self, sql: str, params) -> None:  # noqa: PLR0912
        s = _norm(sql)

        if s.startswith(
            "INSERT INTO business.initiative_recipient_memberships"
        ):
            initiative, contract, recipient, audience, first_seen = params
            initiative_uuid = UUID(initiative)
            recipient_uuid = UUID(recipient)
            # Honor the partial unique index: skip insert if there is an
            # active row for this (initiative, recipient).
            for m in self._s.rows.values():
                if (
                    m["initiative_id"] == initiative_uuid
                    and m["recipient_id"] == recipient_uuid
                    and m["removed_at"] is None
                ):
                    self._row = None
                    return
            m = {
                "id": uuid4(),
                "initiative_id": initiative_uuid,
                "partner_contract_id": UUID(contract),
                "recipient_id": recipient_uuid,
                "data_engine_audience_id": UUID(audience),
                "first_seen_channel_campaign_id": (
                    UUID(first_seen) if first_seen else None
                ),
                "added_at": _now(),
                "removed_at": None,
                "removed_reason": None,
            }
            self._s.rows[m["id"]] = m
            self._row = self._row_tuple(m)
            return

        if (
            s.startswith("SELECT id, initiative_id, partner_contract_id")
            and "FROM business.initiative_recipient_memberships" in s
            and "WHERE initiative_id = %s AND recipient_id = %s"
            " AND removed_at IS NULL" in s
        ):
            initiative_uuid = UUID(params[0])
            recipient_uuid = UUID(params[1])
            for m in self._s.rows.values():
                if (
                    m["initiative_id"] == initiative_uuid
                    and m["recipient_id"] == recipient_uuid
                    and m["removed_at"] is None
                ):
                    self._row = self._row_tuple(m)
                    return
            self._row = None
            return

        if s.startswith(
            "UPDATE business.initiative_recipient_memberships"
        ) and "SET removed_at = NOW()" in s:
            reason, initiative, recipient = params
            initiative_uuid = UUID(initiative)
            recipient_uuid = UUID(recipient)
            for m in self._s.rows.values():
                if (
                    m["initiative_id"] == initiative_uuid
                    and m["recipient_id"] == recipient_uuid
                    and m["removed_at"] is None
                ):
                    m["removed_at"] = _now()
                    m["removed_reason"] = reason
            return

        if (
            s.startswith("SELECT id, initiative_id, partner_contract_id")
            and "WHERE recipient_id = %s AND removed_at IS NULL" in s
        ):
            recipient_uuid = UUID(params[0])
            rows = [
                m
                for m in self._s.rows.values()
                if m["recipient_id"] == recipient_uuid
                and m["removed_at"] is None
            ]
            rows.sort(key=lambda m: m["added_at"], reverse=True)
            self._rows = [self._row_tuple(m) for m in rows]
            return

        if (
            s.startswith("SELECT id, initiative_id, partner_contract_id")
            and "WHERE data_engine_audience_id = %s AND removed_at IS NULL"
            in s
        ):
            audience_uuid = UUID(params[0])
            rows = [
                m
                for m in self._s.rows.values()
                if m["data_engine_audience_id"] == audience_uuid
                and m["removed_at"] is None
            ]
            rows.sort(key=lambda m: m["added_at"], reverse=True)
            self._rows = [self._row_tuple(m) for m in rows]
            return

        if (
            s.startswith("SELECT id, initiative_id, partner_contract_id")
            and "WHERE partner_contract_id = %s AND removed_at IS NULL" in s
        ):
            contract_uuid = UUID(params[0])
            limit, offset = params[1], params[2]
            rows = [
                m
                for m in self._s.rows.values()
                if m["partner_contract_id"] == contract_uuid
                and m["removed_at"] is None
            ]
            rows.sort(key=lambda m: m["added_at"], reverse=True)
            rows = rows[offset : offset + limit]
            self._rows = [self._row_tuple(m) for m in rows]
            return

        if (
            s.startswith("SELECT COUNT(*)")
            and "FROM business.initiative_recipient_memberships" in s
            and "WHERE initiative_id = %s AND removed_at IS NULL" in s
        ):
            initiative_uuid = UUID(params[0])
            n = sum(
                1
                for m in self._s.rows.values()
                if m["initiative_id"] == initiative_uuid
                and m["removed_at"] is None
            )
            self._row = (n,)
            return

        raise AssertionError(f"unhandled SQL: {s}")

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    async def commit(self):
        return None


@pytest.fixture
def store(monkeypatch):
    s = _Store()

    @asynccontextmanager
    async def fake_get_db():
        yield _FakeConn(s)

    monkeypatch.setattr(irm_service, "get_db_connection", fake_get_db)
    return s


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_membership_happy_path(store: _Store) -> None:
    initiative, contract, recipient, audience = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    row = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    assert row["initiative_id"] == initiative
    assert row["partner_contract_id"] == contract
    assert row["recipient_id"] == recipient
    assert row["data_engine_audience_id"] == audience
    assert row["removed_at"] is None
    assert row["first_seen_channel_campaign_id"] is None


@pytest.mark.asyncio
async def test_add_membership_is_idempotent_on_active_row(
    store: _Store,
) -> None:
    """Re-inserting (initiative, recipient) while there's an active row
    returns the existing row rather than creating a duplicate."""
    initiative, contract, recipient, audience = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    first = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    second = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    assert first["id"] == second["id"]
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_add_membership_after_remove_creates_new_active_row(
    store: _Store,
) -> None:
    initiative, contract, recipient, audience = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    first = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    await remove_membership(
        initiative_id=initiative,
        recipient_id=recipient,
        reason="suppression_match",
    )
    second = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    assert first["id"] != second["id"]
    assert len(store.rows) == 2  # one removed, one active


@pytest.mark.asyncio
async def test_remove_membership_marks_active_row_removed(
    store: _Store,
) -> None:
    initiative, contract, recipient, audience = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    await remove_membership(
        initiative_id=initiative,
        recipient_id=recipient,
        reason="unsubscribe",
    )
    rows = await find_active_for_recipient(recipient)
    assert rows == []
    # The underlying row exists, just soft-deleted.
    assert len(store.rows) == 1
    only = next(iter(store.rows.values()))
    assert only["removed_at"] is not None
    assert only["removed_reason"] == "unsubscribe"


@pytest.mark.asyncio
async def test_find_active_for_recipient_filters_removed(
    store: _Store,
) -> None:
    initiative_a, initiative_b = uuid4(), uuid4()
    contract_a, contract_b = uuid4(), uuid4()
    recipient = uuid4()
    audience = uuid4()
    await add_membership(
        initiative_id=initiative_a,
        partner_contract_id=contract_a,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    await add_membership(
        initiative_id=initiative_b,
        partner_contract_id=contract_b,
        recipient_id=recipient,
        data_engine_audience_id=audience,
    )
    await remove_membership(
        initiative_id=initiative_a,
        recipient_id=recipient,
        reason="x",
    )
    rows = await find_active_for_recipient(recipient)
    assert len(rows) == 1
    assert rows[0]["initiative_id"] == initiative_b


@pytest.mark.asyncio
async def test_find_active_by_audience_spec_groups_initiatives(
    store: _Store,
) -> None:
    audience = uuid4()
    other_audience = uuid4()
    init_a, init_b = uuid4(), uuid4()
    contract = uuid4()
    rcpt_a, rcpt_b, rcpt_c = uuid4(), uuid4(), uuid4()
    await add_membership(
        initiative_id=init_a,
        partner_contract_id=contract,
        recipient_id=rcpt_a,
        data_engine_audience_id=audience,
    )
    await add_membership(
        initiative_id=init_b,
        partner_contract_id=contract,
        recipient_id=rcpt_b,
        data_engine_audience_id=audience,
    )
    # Different audience — should NOT be returned.
    await add_membership(
        initiative_id=init_a,
        partner_contract_id=contract,
        recipient_id=rcpt_c,
        data_engine_audience_id=other_audience,
    )
    rows = await find_active_by_audience_spec(audience)
    assert {r["recipient_id"] for r in rows} == {rcpt_a, rcpt_b}


@pytest.mark.asyncio
async def test_list_active_for_contract_filters_removed(
    store: _Store,
) -> None:
    initiative = uuid4()
    contract = uuid4()
    other_contract = uuid4()
    audience = uuid4()
    rcpt_a, rcpt_b, rcpt_c = uuid4(), uuid4(), uuid4()
    await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=rcpt_a,
        data_engine_audience_id=audience,
    )
    await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=rcpt_b,
        data_engine_audience_id=audience,
    )
    await add_membership(
        initiative_id=initiative,
        partner_contract_id=other_contract,
        recipient_id=rcpt_c,
        data_engine_audience_id=audience,
    )
    await remove_membership(
        initiative_id=initiative,
        recipient_id=rcpt_a,
        reason="x",
    )
    rows = await list_active_for_contract(partner_contract_id=contract)
    assert {r["recipient_id"] for r in rows} == {rcpt_b}


@pytest.mark.asyncio
async def test_count_active_for_initiative_excludes_removed(
    store: _Store,
) -> None:
    initiative = uuid4()
    contract = uuid4()
    audience = uuid4()
    r_a, r_b, r_c = uuid4(), uuid4(), uuid4()
    for r in (r_a, r_b, r_c):
        await add_membership(
            initiative_id=initiative,
            partner_contract_id=contract,
            recipient_id=r,
            data_engine_audience_id=audience,
        )
    await remove_membership(
        initiative_id=initiative, recipient_id=r_a, reason="x"
    )
    n = await count_active_for_initiative(initiative)
    assert n == 2


@pytest.mark.asyncio
async def test_add_membership_with_first_seen_channel_campaign(
    store: _Store,
) -> None:
    initiative, contract, recipient, audience, cc = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    row = await add_membership(
        initiative_id=initiative,
        partner_contract_id=contract,
        recipient_id=recipient,
        data_engine_audience_id=audience,
        first_seen_channel_campaign_id=cc,
    )
    assert row["first_seen_channel_campaign_id"] == cc
