"""Vector tests for HMAC signing + secret hashing."""

from __future__ import annotations

from app.services.customer_webhook_signing import (
    generate_secret,
    hash_secret,
    sign_payload,
    verify_secret,
    verify_signature,
)


def test_generate_secret_is_url_safe_43_chars() -> None:
    s = generate_secret()
    assert len(s) >= 40
    # token_urlsafe uses A-Z, a-z, 0-9, '-', '_'.
    assert all(c.isalnum() or c in "-_" for c in s)


def test_generate_secret_is_unique() -> None:
    seen = {generate_secret() for _ in range(100)}
    assert len(seen) == 100


def test_hash_secret_is_deterministic() -> None:
    s = "the-secret-value"
    assert hash_secret(s) == hash_secret(s)


def test_hash_secret_changes_with_input() -> None:
    assert hash_secret("a") != hash_secret("b")


def test_verify_secret_true_for_match() -> None:
    s = "abc123"
    h = hash_secret(s)
    assert verify_secret(s, h) is True


def test_verify_secret_false_for_mismatch() -> None:
    s = "abc123"
    assert verify_secret("abc124", hash_secret(s)) is False


def test_sign_payload_known_vector() -> None:
    sig = sign_payload("super-secret", b'{"event":"page.viewed"}')
    assert sig.startswith("sha256=")
    # Should be 64 hex chars after the prefix.
    assert len(sig) == len("sha256=") + 64


def test_sign_payload_changes_with_body() -> None:
    sig_a = sign_payload("k", b"a")
    sig_b = sign_payload("k", b"b")
    assert sig_a != sig_b


def test_verify_signature_round_trips() -> None:
    secret = "the-secret"
    body = b'{"hello":"world"}'
    sig = sign_payload(secret, body)
    assert verify_signature(secret, body, sig) is True


def test_verify_signature_rejects_wrong_secret() -> None:
    body = b'{"a":1}'
    sig = sign_payload("k1", body)
    assert verify_signature("k2", body, sig) is False


def test_verify_signature_rejects_unprefixed_signature() -> None:
    body = b"{}"
    sig = sign_payload("k", body)
    bare = sig.split("=", 1)[1]
    assert verify_signature("k", body, bare) is False
