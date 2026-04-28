"""Pre-send US address-verify gate."""

from __future__ import annotations

import pytest

from app.direct_mail import addresses
from app.providers.lob.client import LobProviderError


@pytest.fixture
def stub_db(monkeypatch):
    """Default: nothing suppressed; insertions noop. Overridable per test."""
    state = {"suppressed_hashes": set(), "inserted": []}

    async def fake_is_suppressed(address_hash):
        if address_hash in state["suppressed_hashes"]:
            return {"id": "x", "reason": "manual", "suppressed_at": "now", "notes": None}
        return None

    async def fake_insert(*, address, reason, **kwargs):
        h = addresses.address_hash_for(address)
        state["inserted"].append((h, reason))
        state["suppressed_hashes"].add(h)
        return True

    monkeypatch.setattr(addresses, "is_address_suppressed", fake_is_suppressed)
    monkeypatch.setattr(addresses, "insert_suppression", fake_insert)
    return state


@pytest.fixture
def stub_lob_verify(monkeypatch):
    state = {"calls": [], "result": {"deliverability": "deliverable"}}

    def fake_verify(api_key, payload, **kwargs):
        state["calls"].append((api_key, payload))
        return state["result"]

    monkeypatch.setattr(addresses.lob_client, "verify_address_us_single", fake_verify)
    return state


@pytest.mark.asyncio
async def test_saved_address_id_skips_gate(stub_db, stub_lob_verify):
    result = await addresses.verify_or_suppress(
        api_key="k",
        payload={"to": "adr_xyz"},
        skip=False,
    )
    assert result.deliverability is None
    assert stub_lob_verify["calls"] == []


@pytest.mark.asyncio
async def test_skip_flag_bypasses_verify(stub_db, stub_lob_verify):
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    result = await addresses.verify_or_suppress(api_key="k", payload=payload, skip=True)
    assert result.deliverability is None
    assert stub_lob_verify["calls"] == []


@pytest.mark.asyncio
async def test_deliverable_proceeds_and_returns_verdict(stub_db, stub_lob_verify):
    stub_lob_verify["result"] = {"deliverability": "deliverable"}
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    result = await addresses.verify_or_suppress(api_key="k", payload=payload, skip=False)
    assert result.deliverability == "deliverable"
    assert len(stub_lob_verify["calls"]) == 1
    assert stub_db["inserted"] == []


@pytest.mark.asyncio
async def test_undeliverable_rejects_and_suppresses(stub_db, stub_lob_verify):
    stub_lob_verify["result"] = {"deliverability": "undeliverable"}
    payload = {
        "to": {
            "address_line1": "999 Bad",
            "address_city": "Nowhere",
            "address_state": "ZZ",
            "address_zip": "00000",
        }
    }
    with pytest.raises(addresses.AddressUndeliverable) as exc:
        await addresses.verify_or_suppress(api_key="k", payload=payload, skip=False)
    assert exc.value.deliverability == "undeliverable"
    assert len(stub_db["inserted"]) == 1
    assert stub_db["inserted"][0][1] == "undeliverable_at_send"


@pytest.mark.asyncio
async def test_already_suppressed_blocks_send(stub_db, stub_lob_verify):
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    h = addresses.address_hash_for(payload["to"])
    stub_db["suppressed_hashes"].add(h)
    with pytest.raises(addresses.AddressSuppressed) as exc:
        await addresses.verify_or_suppress(api_key="k", payload=payload, skip=False)
    assert exc.value.address_hash == h
    assert stub_lob_verify["calls"] == []


@pytest.mark.asyncio
async def test_provider_error_fails_open(stub_db, stub_lob_verify, monkeypatch):
    def fake_verify(*args, **kwargs):
        raise LobProviderError("Lob connectivity error: simulated")

    monkeypatch.setattr(addresses.lob_client, "verify_address_us_single", fake_verify)
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    result = await addresses.verify_or_suppress(api_key="k", payload=payload, skip=False)
    assert result.deliverability is None
