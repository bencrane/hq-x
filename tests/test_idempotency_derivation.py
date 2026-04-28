"""Auto-derivation of Lob idempotency keys."""

from __future__ import annotations

from app.providers.lob.idempotency import derive_idempotency_key


def test_same_payload_same_key():
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    assert derive_idempotency_key(piece_type="postcard", payload=payload) == derive_idempotency_key(
        piece_type="postcard", payload=payload
    )


def test_different_recipient_different_key():
    base = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    other = {
        "to": {
            "address_line1": "2 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    assert derive_idempotency_key(piece_type="postcard", payload=base) != derive_idempotency_key(
        piece_type="postcard", payload=other
    )


def test_different_piece_type_different_key():
    payload = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    assert derive_idempotency_key(piece_type="postcard", payload=payload) != derive_idempotency_key(
        piece_type="letter", payload=payload
    )


def test_case_and_whitespace_collapse():
    a = {
        "to": {
            "address_line1": "1 main st",
            "address_city": "sf",
            "address_state": "ca",
            "address_zip": "94101",
        }
    }
    b = {
        "to": {
            "address_line1": "  1 MAIN ST  ",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    assert derive_idempotency_key(piece_type="postcard", payload=a) == derive_idempotency_key(
        piece_type="postcard", payload=b
    )


def test_mutable_fields_collapse():
    base = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        },
        "front": "tmpl_x",
    }
    with_metadata = {
        **base,
        "description": "marketing run",
        "metadata": {"campaign": "abc"},
        "send_date": "2026-05-01",
    }
    assert derive_idempotency_key(piece_type="postcard", payload=base) == derive_idempotency_key(
        piece_type="postcard", payload=with_metadata
    )


def test_dedup_tag_changes_key():
    base = {
        "to": {
            "address_line1": "1 Main",
            "address_city": "SF",
            "address_state": "CA",
            "address_zip": "94101",
        }
    }
    tagged = {**base, "dedup_tag": "run-2"}
    assert derive_idempotency_key(piece_type="postcard", payload=base) != derive_idempotency_key(
        piece_type="postcard", payload=tagged
    )


def test_saved_address_id_works_as_recipient():
    payload = {"to": "adr_xyz", "front": "tmpl_x"}
    key1 = derive_idempotency_key(piece_type="postcard", payload=payload)
    key2 = derive_idempotency_key(piece_type="postcard", payload=payload)
    assert key1 == key2
    assert key1.startswith("hqx-v1-postcard-")


def test_template_changes_key():
    base = {"to": "adr_x", "front": "tmpl_a"}
    other = {"to": "adr_x", "front": "tmpl_b"}
    assert derive_idempotency_key(piece_type="postcard", payload=base) != derive_idempotency_key(
        piece_type="postcard", payload=other
    )
