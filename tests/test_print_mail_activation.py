"""Tests for `app.services.print_mail_activation`.

Mocks the Lob client + the suppression DB lookup + upsert_piece. Real
HTTP and real Postgres are out of scope; the seed script exercises both
end-to-end against Lob test mode (see scripts/seed_print_mail_batch_demo.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.direct_mail.persistence import UpsertedPiece
from app.providers.lob.client import LobProviderError
from app.services import print_mail_activation as pma_module
from app.services.print_mail_activation import (
    ActivationBatchResult,
    BookletSpec,
    LetterSpec,
    PieceSpec,
    PostcardSpec,
    SelfMailerSpec,
    SnapPackSpec,
    activate_pieces_batch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_ORG_ID = UUID("11111111-1111-1111-1111-111111111111")


def _addr(line1: str = "1 Main", zip5: str = "94101") -> dict[str, Any]:
    return {
        "name": "Recipient",
        "address_line1": line1,
        "address_city": "SF",
        "address_state": "CA",
        "address_zip": zip5,
        "address_country": "US",
    }


def _from_addr() -> dict[str, Any]:
    return {
        "name": "Sender",
        "address_line1": "100 Sender St",
        "address_city": "Oakland",
        "address_state": "CA",
        "address_zip": "94607",
        "address_country": "US",
    }


def _make_postcard(seed: str = "seed-postcard", **overrides: Any) -> PostcardSpec:
    base = dict(
        piece_type="postcard",
        to=_addr(),
        front="<html>front</html>",
        back="<html>back</html>",
        idempotency_seed=seed,
    )
    base["from"] = _from_addr()
    base.update(overrides)
    return PostcardSpec(**base)


def _make_self_mailer(
    seed: str = "seed-sfm", **overrides: Any
) -> SelfMailerSpec:
    base = dict(
        piece_type="self_mailer",
        to=_addr(),
        inside="<html>inside</html>",
        outside="<html>outside</html>",
        idempotency_seed=seed,
    )
    base["from"] = _from_addr()
    base.update(overrides)
    return SelfMailerSpec(**base)


def _make_letter(seed: str = "seed-letter", **overrides: Any) -> LetterSpec:
    base = dict(
        piece_type="letter",
        to=_addr(),
        file="<html>letter</html>",
        idempotency_seed=seed,
    )
    base["from"] = _from_addr()
    base.update(overrides)
    return LetterSpec(**base)


def _make_snap_pack(
    seed: str = "seed-snap", **overrides: Any
) -> SnapPackSpec:
    base = dict(
        piece_type="snap_pack",
        to=_addr(),
        inside="<html>snap-inside</html>",
        outside="<html>snap-outside</html>",
        idempotency_seed=seed,
    )
    base["from"] = _from_addr()
    base.update(overrides)
    return SnapPackSpec(**base)


def _make_booklet(
    seed: str = "seed-booklet", **overrides: Any
) -> BookletSpec:
    base = dict(
        piece_type="booklet",
        to=_addr(),
        file="<html>booklet</html>",
        idempotency_seed=seed,
    )
    base["from"] = _from_addr()
    base.update(overrides)
    return BookletSpec(**base)


_MAKER = {
    "postcard": _make_postcard,
    "self_mailer": _make_self_mailer,
    "letter": _make_letter,
    "snap_pack": _make_snap_pack,
    "booklet": _make_booklet,
}


@pytest.fixture
def stub_suppression(monkeypatch):
    """Default: nothing is suppressed."""
    state = {"suppressed_hashes": set()}

    async def fake_is_suppressed(address_hash):
        if address_hash in state["suppressed_hashes"]:
            return {"id": "x", "reason": "manual", "suppressed_at": "now", "notes": None}
        return None

    monkeypatch.setattr(pma_module, "is_address_suppressed", fake_is_suppressed)
    return state


@pytest.fixture
def stub_persistence(monkeypatch):
    """Capture every upsert_piece call and replay a fake row."""
    state = {"calls": []}

    async def fake_upsert(*, piece_type, provider_piece, deliverability,
                          created_by_user_id, is_test_mode=False,
                          metadata=None, provider_slug="lob",
                          channel_campaign_step_id=None,
                          channel_campaign_id=None,
                          campaign_id=None,
                          recipient_id=None):
        state["calls"].append({
            "piece_type": piece_type,
            "provider_piece": provider_piece,
            "is_test_mode": is_test_mode,
            "metadata": metadata,
            "provider_slug": provider_slug,
        })
        return UpsertedPiece(
            id=uuid4(),
            external_piece_id=provider_piece["id"],
            piece_type=piece_type,
            status=provider_piece.get("status", "queued"),
            cost_cents=None,
            deliverability=deliverability,
            is_test_mode=is_test_mode,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            raw_payload=provider_piece,
            metadata=metadata,
        )

    monkeypatch.setattr(pma_module, "upsert_piece", fake_upsert)
    return state


@pytest.fixture
def stub_lob(monkeypatch):
    """Stub all five Lob create_* functions on the lob_client module the
    activation service imported."""

    state = {
        "calls": [],
        "responses": {
            "postcard": {"id": "psc_test_001", "price": "0.84", "status": "queued"},
            "self_mailer": {"id": "sfm_test_001", "price": "0.50", "status": "queued"},
            "letter": {"id": "ltr_test_001", "price": "1.40", "status": "queued"},
            "snap_pack": {"id": "ord_test_001", "price": "0.60", "status": "queued"},
            "booklet": {"id": "bkl_test_001", "price": "1.20", "status": "queued"},
        },
        "raise_for": {},  # piece_type -> exception instance to raise
    }

    def _factory(piece_type: str):
        def _fake(api_key, payload, *, idempotency_key=None, idempotency_in_query=False,
                 base_url=None, timeout_seconds=12.0, **kwargs):
            state["calls"].append({
                "piece_type": piece_type,
                "api_key": api_key,
                "payload": payload,
                "idempotency_key": idempotency_key,
            })
            exc = state["raise_for"].get(piece_type)
            if exc is not None:
                raise exc
            return state["responses"][piece_type]
        return _fake

    for piece_type, fn_name in (
        ("postcard", "create_postcard"),
        ("self_mailer", "create_self_mailer"),
        ("letter", "create_letter"),
        ("snap_pack", "create_snap_pack"),
        ("booklet", "create_booklet"),
    ):
        monkeypatch.setattr(pma_module.lob_client, fn_name, _factory(piece_type))

    return state


# ---------------------------------------------------------------------------
# Per-type dispatch — one happy-path test per type, asserting:
#   1. the right Lob create_* was called with the type-specific payload shape
#   2. the response's id round-trips into the persisted row with the right
#      type and provider_slug='lob'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_dispatches_postcard(stub_suppression, stub_persistence, stub_lob):
    spec = _make_postcard(size="6x9")
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1 and result.failed == 0 and result.skipped == 0
    assert len(stub_lob["calls"]) == 1
    call = stub_lob["calls"][0]
    assert call["piece_type"] == "postcard"
    payload = call["payload"]
    assert payload["front"] == "<html>front</html>"
    assert payload["back"] == "<html>back</html>"
    assert payload["size"] == "6x9"
    assert "inside" not in payload and "file" not in payload
    upsert = stub_persistence["calls"][0]
    assert upsert["piece_type"] == "postcard"
    assert upsert["provider_slug"] == "lob"
    assert result.results[0].external_piece_id.startswith("psc_")


@pytest.mark.asyncio
async def test_batch_dispatches_self_mailer(stub_suppression, stub_persistence, stub_lob):
    spec = _make_self_mailer(size="6x18_bifold")
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1
    call = stub_lob["calls"][0]
    assert call["piece_type"] == "self_mailer"
    payload = call["payload"]
    assert payload["inside"] == "<html>inside</html>"
    assert payload["outside"] == "<html>outside</html>"
    assert payload["size"] == "6x18_bifold"
    # self_mailer must NOT carry front/back/file in the Lob payload
    assert "front" not in payload and "back" not in payload and "file" not in payload
    assert result.results[0].external_piece_id.startswith("sfm_")


@pytest.mark.asyncio
async def test_batch_dispatches_letter(stub_suppression, stub_persistence, stub_lob):
    spec = _make_letter(
        color=True,
        double_sided=False,
        address_placement="insert_blank_page",
        extra_service="certified",
    )
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1
    call = stub_lob["calls"][0]
    assert call["piece_type"] == "letter"
    payload = call["payload"]
    assert payload["file"] == "<html>letter</html>"
    assert payload["color"] is True
    assert payload["double_sided"] is False
    assert payload["address_placement"] == "insert_blank_page"
    assert payload["extra_service"] == "certified"
    # letter must NOT carry front/back/inside/outside in the Lob payload
    for forbidden in ("front", "back", "inside", "outside"):
        assert forbidden not in payload, forbidden
    assert result.results[0].external_piece_id.startswith("ltr_")


@pytest.mark.asyncio
async def test_batch_dispatches_snap_pack(stub_suppression, stub_persistence, stub_lob):
    spec = _make_snap_pack()
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1
    call = stub_lob["calls"][0]
    assert call["piece_type"] == "snap_pack"
    payload = call["payload"]
    assert payload["inside"] == "<html>snap-inside</html>"
    assert payload["outside"] == "<html>snap-outside</html>"
    assert payload["size"] == "8.5x11"
    # snap_pack id prefix is `ord_` per Lob's docs (NOT `snp_`)
    assert result.results[0].external_piece_id.startswith("ord_")


@pytest.mark.asyncio
async def test_batch_dispatches_booklet(stub_suppression, stub_persistence, stub_lob):
    spec = _make_booklet(size="8.25x5.5")
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1
    call = stub_lob["calls"][0]
    assert call["piece_type"] == "booklet"
    payload = call["payload"]
    assert payload["file"] == "<html>booklet</html>"
    assert payload["size"] == "8.25x5.5"
    for forbidden in ("front", "back", "inside", "outside"):
        assert forbidden not in payload, forbidden
    assert result.results[0].external_piece_id.startswith("bkl_")


# ---------------------------------------------------------------------------
# Per-type negative tests — Pydantic refuses cross-type field shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec_cls,kwargs,reason",
    [
        # postcard rejects inside/outside (those belong on self_mailer / snap_pack)
        (
            PostcardSpec,
            dict(piece_type="postcard", inside="x", outside="y", front="f", back="b"),
            "postcard with inside/outside",
        ),
        # letter rejects front/back (those belong on postcard)
        (
            LetterSpec,
            dict(piece_type="letter", front="f", back="b", file="x"),
            "letter with front/back",
        ),
        # self_mailer rejects file (that belongs on letter / booklet)
        (
            SelfMailerSpec,
            dict(piece_type="self_mailer", file="x", inside="i", outside="o"),
            "self_mailer with file",
        ),
        # booklet rejects inside/outside
        (
            BookletSpec,
            dict(piece_type="booklet", inside="i", outside="o", file="x"),
            "booklet with inside/outside",
        ),
        # snap_pack rejects file
        (
            SnapPackSpec,
            dict(piece_type="snap_pack", file="x", inside="i", outside="o"),
            "snap_pack with file",
        ),
    ],
)
def test_spec_rejects_wrong_artwork_fields(spec_cls, kwargs, reason):
    base = {
        "to": _addr(),
        "from": _from_addr(),
        "idempotency_seed": "x",
        **kwargs,
    }
    with pytest.raises(ValidationError):
        spec_cls(**base)


@pytest.mark.parametrize(
    "spec_cls,bad_size",
    [
        (PostcardSpec, "11x9_bifold"),
        (PostcardSpec, "8.5x11"),
        (SelfMailerSpec, "4x6"),
        (BookletSpec, "4x6"),
        (BookletSpec, "8.5x11"),
    ],
)
def test_size_enum_per_type_is_enforced(spec_cls, bad_size):
    base: dict[str, Any] = {
        "to": _addr(),
        "from": _from_addr(),
        "idempotency_seed": "x",
        "size": bad_size,
    }
    if spec_cls is PostcardSpec:
        base.update({"piece_type": "postcard", "front": "f", "back": "b"})
    elif spec_cls is SelfMailerSpec:
        base.update({"piece_type": "self_mailer", "inside": "i", "outside": "o"})
    elif spec_cls is BookletSpec:
        base.update({"piece_type": "booklet", "file": "f"})
    with pytest.raises(ValidationError):
        spec_cls(**base)


# ---------------------------------------------------------------------------
# Cross-type behavior tests
# ---------------------------------------------------------------------------


def _all_five_specs() -> list[PieceSpec]:
    """One spec of each type with distinct seeds + addresses to keep
    suppression / idempotency independent."""
    specs = []
    for idx, piece_type in enumerate(
        ["postcard", "self_mailer", "letter", "snap_pack", "booklet"]
    ):
        spec = _MAKER[piece_type](
            seed=f"seed-{piece_type}-{idx}",
            to=_addr(line1=f"{idx} Main"),
        )
        specs.append(spec)
    return specs


@pytest.mark.asyncio
async def test_batch_mixes_all_five_types_in_one_call(
    stub_suppression, stub_persistence, stub_lob
):
    specs = _all_five_specs()
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=specs, test_mode=True,
    )
    assert result.total == 5
    assert result.created == 5
    assert result.failed == 0
    assert result.skipped == 0
    # Each type's create_* was called exactly once, with the right shape.
    by_type = {c["piece_type"] for c in stub_lob["calls"]}
    assert by_type == {"postcard", "self_mailer", "letter", "snap_pack", "booklet"}
    # All five rows persisted with the right piece_type.
    persisted_types = sorted(c["piece_type"] for c in stub_persistence["calls"])
    assert persisted_types == sorted(["postcard", "self_mailer", "letter", "snap_pack", "booklet"])
    # External ids round-trip with the right Lob prefix.
    prefixes = {r.piece_type: r.external_piece_id[:4] for r in result.results}
    assert prefixes["postcard"] == "psc_"
    assert prefixes["self_mailer"] == "sfm_"
    assert prefixes["letter"] == "ltr_"
    assert prefixes["snap_pack"] == "ord_"
    assert prefixes["booklet"] == "bkl_"


@pytest.mark.asyncio
async def test_batch_continues_on_single_failure(
    stub_suppression, stub_persistence, stub_lob
):
    specs = _all_five_specs()
    stub_lob["raise_for"]["letter"] = LobProviderError(
        "Lob API returned HTTP 422: bad merge variable"
    )

    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=specs, test_mode=True,
    )
    assert result.total == 5
    assert result.created == 4
    assert result.failed == 1
    assert result.skipped == 0
    failed = [r for r in result.results if r.status == "failed"]
    assert len(failed) == 1 and failed[0].piece_type == "letter"
    # The four other types were persisted; the failed letter was not.
    assert len(stub_persistence["calls"]) == 4
    assert "letter" not in {c["piece_type"] for c in stub_persistence["calls"]}


@pytest.mark.asyncio
async def test_batch_skips_suppressed_address(
    stub_suppression, stub_persistence, stub_lob
):
    # Suppress the snap_pack's address by hash
    from app.direct_mail.addresses import address_hash_for

    snap_addr = _addr(line1="0 Main")  # Matches first spec in _all_five_specs() (idx=0)
    # build five specs but force snap_pack (idx=3) to use the hash we'll suppress
    specs: list[PieceSpec] = []
    for idx, piece_type in enumerate(
        ["postcard", "self_mailer", "letter", "snap_pack", "booklet"]
    ):
        if piece_type == "snap_pack":
            specs.append(
                _MAKER[piece_type](
                    seed=f"seed-{piece_type}-{idx}",
                    to=snap_addr,
                )
            )
        else:
            specs.append(
                _MAKER[piece_type](
                    seed=f"seed-{piece_type}-{idx}",
                    to=_addr(line1=f"{idx + 100} Main"),
                )
            )
    stub_suppression["suppressed_hashes"].add(address_hash_for(snap_addr))

    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=specs, test_mode=True,
    )
    assert result.total == 5
    assert result.created == 4
    assert result.skipped == 1
    assert result.failed == 0
    # The snap_pack was skipped — no Lob call for it.
    snap_calls = [c for c in stub_lob["calls"] if c["piece_type"] == "snap_pack"]
    assert snap_calls == []
    skipped = [r for r in result.results if r.status == "skipped_suppressed"]
    assert len(skipped) == 1 and skipped[0].piece_type == "snap_pack"
    assert skipped[0].error_code == "suppressed"


@pytest.mark.parametrize(
    "piece_type", ["postcard", "self_mailer", "letter", "snap_pack", "booklet"]
)
@pytest.mark.asyncio
async def test_batch_idempotency_key_is_deterministic_from_seed(
    piece_type, stub_suppression, stub_persistence, stub_lob
):
    seed = f"deterministic-seed-for-{piece_type}"
    spec = _MAKER[piece_type](seed=seed)
    result1 = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    spec2 = _MAKER[piece_type](seed=seed)
    result2 = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec2], test_mode=True,
    )
    assert result1.created == 1 and result2.created == 1
    keys = [
        c["idempotency_key"]
        for c in stub_lob["calls"]
        if c["piece_type"] == piece_type
    ]
    assert len(keys) == 2
    assert keys[0] == keys[1], (
        f"idempotency keys for the same seed must collide; got {keys}"
    )


@pytest.mark.parametrize(
    "piece_type", ["postcard", "self_mailer", "letter", "snap_pack", "booklet"]
)
@pytest.mark.asyncio
async def test_batch_persists_back_references_in_metadata(
    piece_type, stub_suppression, stub_persistence, stub_lob
):
    rid = uuid4()
    sid = uuid4()
    mid = uuid4()
    spec = _MAKER[piece_type](
        recipient_id=rid,
        channel_campaign_step_id=sid,
        membership_id=mid,
    )
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 1
    upsert = stub_persistence["calls"][0]
    md = upsert["metadata"] or {}
    assert md.get("_recipient_id") == str(rid)
    assert md.get("_channel_campaign_step_id") == str(sid)
    assert md.get("_membership_id") == str(mid)


@pytest.mark.asyncio
async def test_batch_persistence_failure_after_lob_success(
    stub_suppression, monkeypatch, stub_lob
):
    """upsert_piece raises after a successful Lob create — surface the
    Lob external piece id so the operator can reconcile."""

    async def fake_upsert(**kwargs):
        raise RuntimeError("simulated DB unavailable")

    monkeypatch.setattr(pma_module, "upsert_piece", fake_upsert)
    spec = _make_postcard()
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=[spec], test_mode=True,
    )
    assert result.created == 0
    assert result.failed == 1
    pr = result.results[0]
    assert pr.status == "failed"
    assert pr.error_code == "persistence_failed"
    assert pr.error_detail and pr.error_detail.get("lob_external_piece_id") == "psc_test_001"


@pytest.mark.asyncio
async def test_batch_concurrency_is_bounded_at_eight(
    stub_suppression, stub_persistence, monkeypatch
):
    """Submit 32 pieces; assert at most 8 are in-flight at any moment."""

    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_create_in_thread(create_fn, *args, **kwargs):
        # asyncio.to_thread shim that lets us instrument concurrency
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.01)
            # Simulate Lob's success response for postcard
            return {"id": f"psc_inflight_{uuid4().hex[:8]}", "status": "queued"}
        finally:
            async with lock:
                inflight -= 1

    # Replace asyncio.to_thread on the activation module's namespace
    async def fake_to_thread(fn, *args, **kwargs):
        return await fake_create_in_thread(fn, *args, **kwargs)

    monkeypatch.setattr(pma_module.asyncio, "to_thread", fake_to_thread)

    specs = [_make_postcard(seed=f"inflight-{i}") for i in range(32)]
    result = await activate_pieces_batch(
        organization_id=_ORG_ID, pieces=specs, test_mode=True,
    )
    assert result.created == 32
    assert peak <= 8, f"saw {peak} pieces in flight; cap is 8"


@pytest.mark.asyncio
async def test_batch_correlation_id_round_trips(
    stub_suppression, stub_persistence, stub_lob
):
    spec = _make_postcard()
    result = await activate_pieces_batch(
        organization_id=_ORG_ID,
        pieces=[spec],
        test_mode=True,
        correlation_id="caller-supplied-corr",
    )
    assert result.correlation_id == "caller-supplied-corr"

    # Absent id is auto-generated; same single batch carries one stable id.
    result2 = await activate_pieces_batch(
        organization_id=_ORG_ID,
        pieces=[spec, _make_letter()],
        test_mode=True,
    )
    assert result2.correlation_id and len(result2.correlation_id) >= 16


# ---------------------------------------------------------------------------
# ActivationBatchResult is a useful smoke for callers
# ---------------------------------------------------------------------------


def test_activation_batch_result_is_pydantic_model():
    # Bare construction smoke; ensures the public type is constructible.
    result = ActivationBatchResult(
        correlation_id="x", total=0, created=0, skipped=0, failed=0, results=[]
    )
    assert result.total == 0


# Some quiet-down for unused imports under linters:
_ = contextlib
