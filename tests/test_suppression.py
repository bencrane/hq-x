"""Suppression list — hash normalization + insert-with-conflict + check."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.direct_mail import addresses


@pytest.fixture
def fake_db(monkeypatch):
    rows: list[dict] = []
    suppressed_ids = {"counter": 0}

    class FakeCursor:
        def __init__(self):
            self._last_returning: object | None = None

        async def execute(self, sql: str, params: tuple):
            sql_lower = sql.strip().lower()
            if "insert into suppressed_addresses" in sql_lower:
                key = (params[0], params[6])  # (address_hash, reason)
                existing = next(
                    (r for r in rows if (r["address_hash"], r["reason"]) == key),
                    None,
                )
                if existing is not None:
                    self._last_returning = None
                else:
                    new_id = uuid4()
                    rows.append(
                        {
                            "id": new_id,
                            "address_hash": params[0],
                            "address_line1": params[1],
                            "address_line2": params[2],
                            "address_city": params[3],
                            "address_state": params[4],
                            "address_zip": params[5],
                            "reason": params[6],
                            "source_event_id": params[7],
                            "source_piece_id": params[8],
                            "notes": params[9],
                            "suppressed_at": "now",
                        }
                    )
                    self._last_returning = (new_id,)
                suppressed_ids["counter"] += 1
            elif "select id, reason, suppressed_at, notes" in sql_lower:
                addr_hash = params[0]
                hits = [r for r in rows if r["address_hash"] == addr_hash]
                if hits:
                    r = hits[-1]
                    self._last_returning = (r["id"], r["reason"], r["suppressed_at"], r["notes"])
                else:
                    self._last_returning = None
            else:
                raise NotImplementedError(sql)

        async def fetchone(self):
            return self._last_returning

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    @asynccontextmanager
    async def fake_get_db_connection():
        async with FakeConn() as conn:
            yield conn

    monkeypatch.setattr(addresses, "get_db_connection", fake_get_db_connection)
    return rows


def test_normalize_collapses_case_and_whitespace():
    a = addresses.normalize_address(
        {
            "address_line1": "1 Main St",
            "address_city": "San Francisco",
            "address_state": "CA",
            "address_zip": "94101",
        }
    )
    b = addresses.normalize_address(
        {
            "address_line1": "  1 main st  ",
            "address_city": "san francisco",
            "address_state": "ca",
            "address_zip": "94101-1234",
        }
    )
    assert a.address_hash == b.address_hash
    assert b.zip5 == "94101"


def test_normalize_distinguishes_different_lines():
    a = addresses.normalize_address(
        {
            "address_line1": "1 Main St",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    )
    b = addresses.normalize_address(
        {
            "address_line1": "2 Main St",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    )
    assert a.address_hash != b.address_hash


@pytest.mark.asyncio
async def test_insert_suppression_inserts_then_dedups(fake_db):
    addr = {
        "address_line1": "1 Main",
        "address_city": "SF",
        "address_state": "CA",
        "address_zip": "94101",
    }
    inserted_first = await addresses.insert_suppression(address=addr, reason="returned_to_sender")
    inserted_second = await addresses.insert_suppression(address=addr, reason="returned_to_sender")
    assert inserted_first is True
    assert inserted_second is False
    assert len(fake_db) == 1


@pytest.mark.asyncio
async def test_insert_suppression_distinct_reason_does_not_dedup(fake_db):
    addr = {
        "address_line1": "1 Main",
        "address_city": "SF",
        "address_state": "CA",
        "address_zip": "94101",
    }
    a = await addresses.insert_suppression(address=addr, reason="returned_to_sender")
    b = await addresses.insert_suppression(address=addr, reason="failed")
    assert a and b
    assert len(fake_db) == 2


@pytest.mark.asyncio
async def test_is_address_suppressed_finds_row(fake_db):
    addr = {
        "address_line1": "1 Main",
        "address_city": "SF",
        "address_state": "CA",
        "address_zip": "94101",
    }
    await addresses.insert_suppression(address=addr, reason="manual", notes="bad neighborhood")
    h = addresses.address_hash_for(addr)
    found = await addresses.is_address_suppressed(h)
    assert found is not None
    assert found["reason"] == "manual"


@pytest.mark.asyncio
async def test_is_address_suppressed_misses(fake_db):
    found = await addresses.is_address_suppressed("0" * 64)
    assert found is None


@pytest.mark.asyncio
async def test_thin_address_skipped(fake_db):
    inserted = await addresses.insert_suppression(
        address={"address_line1": "", "address_city": "", "address_state": "", "address_zip": ""},
        reason="returned_to_sender",
    )
    assert inserted is False
    assert fake_db == []
