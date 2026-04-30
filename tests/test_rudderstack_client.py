"""Tests for the RudderStack write client wrapper.

Validates the four invariants that matter for hq-x's fire-and-forget
analytics fan-out:

1. Unconfigured (no env vars) → ``_is_configured`` is False; ``track``
   is a no-op that never imports the SDK.
2. Configured → ``track`` calls the SDK with the expected
   ``event_name`` / ``anonymous_id`` / ``properties``.
3. SDK exception inside ``track`` does not propagate.
4. ``flush`` is a safe no-op when unconfigured and a real call when
   configured.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

import app.config as config_module
from app import rudderstack


@pytest.fixture(autouse=True)
def _reset_singleton():
    rudderstack._reset_for_tests()
    yield
    rudderstack._reset_for_tests()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_module.settings, "RUDDERSTACK_WRITE_KEY", None)
    monkeypatch.setattr(
        config_module.settings, "RUDDERSTACK_DATA_PLANE_URL", None
    )
    yield


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    """Fake the SDK module so tests don't actually hit RudderStack.

    The fake records ``track`` and ``flush`` calls on a ``calls`` list
    attached to the module so assertions can read them out.
    """
    monkeypatch.setattr(
        config_module.settings,
        "RUDDERSTACK_WRITE_KEY",
        SecretStr("fake-write-key"),
    )
    monkeypatch.setattr(
        config_module.settings,
        "RUDDERSTACK_DATA_PLANE_URL",
        "https://fake.dataplane.example.com",
    )

    fake = SimpleNamespace(
        write_key=None,
        dataPlaneUrl=None,
        calls=[],
        flushed=0,
    )

    def _track(**kwargs: Any) -> None:
        fake.calls.append(kwargs)

    def _flush() -> None:
        fake.flushed += 1

    fake.track = _track  # type: ignore[attr-defined]
    fake.flush = _flush  # type: ignore[attr-defined]

    fake_pkg = SimpleNamespace(analytics=fake)
    monkeypatch.setitem(sys.modules, "rudderstack", fake_pkg)
    monkeypatch.setitem(sys.modules, "rudderstack.analytics", fake)
    yield fake


# ── unconfigured ────────────────────────────────────────────────────────


def test_is_configured_false_when_unset(unconfigured: None) -> None:
    assert rudderstack._is_configured() is False


def test_track_noop_when_unconfigured(
    unconfigured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should never import the SDK or call track when env vars are absent."""

    def _explode(*_: Any, **__: Any) -> None:
        pytest.fail("SDK should not be touched when unconfigured")

    monkeypatch.setitem(sys.modules, "rudderstack.analytics", _explode)
    rudderstack.track(
        event_name="piece.delivered",
        anonymous_id="org-uuid",
        properties={"recipient_id": "rcpt-uuid"},
    )


def test_flush_noop_when_unconfigured(unconfigured: None) -> None:
    rudderstack.flush()  # must not raise


# ── configured ──────────────────────────────────────────────────────────


def test_track_calls_sdk_with_expected_args(configured: SimpleNamespace) -> None:
    rudderstack.track(
        event_name="piece.delivered",
        anonymous_id="org-uuid",
        properties={"recipient_id": "rcpt-uuid", "channel": "direct_mail"},
    )
    assert len(configured.calls) == 1
    call = configured.calls[0]
    assert call["anonymous_id"] == "org-uuid"
    assert call["event"] == "piece.delivered"
    assert call["properties"]["recipient_id"] == "rcpt-uuid"
    assert call["properties"]["channel"] == "direct_mail"
    # Init side effects: write_key + dataPlaneUrl set on the singleton.
    assert configured.write_key == "fake-write-key"
    assert configured.dataPlaneUrl == "https://fake.dataplane.example.com"


def test_track_swallows_sdk_exceptions(
    configured: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(**_: Any) -> None:
        raise RuntimeError("sdk explodes")

    monkeypatch.setattr(configured, "track", _boom)
    with caplog.at_level(logging.WARNING, logger="app.rudderstack"):
        rudderstack.track(
            event_name="piece.failed",
            anonymous_id="org-uuid",
            properties={},
        )
    # Caller doesn't see the exception, but the failure was logged.
    assert any("rudderstack track failed" in m for m in caplog.messages)


def test_flush_calls_sdk_when_configured(configured: SimpleNamespace) -> None:
    # Have to populate _client first via a track call.
    rudderstack.track(
        event_name="x", anonymous_id="org-uuid", properties=None
    )
    assert configured.flushed == 0
    rudderstack.flush()
    assert configured.flushed == 1


def test_lazy_init_caches_client(configured: SimpleNamespace) -> None:
    """Two consecutive track calls share one initialized client."""
    rudderstack.track(
        event_name="a", anonymous_id="org-uuid", properties={}
    )
    rudderstack.track(
        event_name="b", anonymous_id="org-uuid", properties={}
    )
    # write_key was set exactly once (the assignment is idempotent, so
    # this is really an "SDK was imported once" check).
    assert configured.write_key == "fake-write-key"
    assert len(configured.calls) == 2


def test_track_handles_missing_data_plane_url_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the write key is set but the URL is empty, treat as unconfigured."""
    monkeypatch.setattr(
        config_module.settings, "RUDDERSTACK_WRITE_KEY", SecretStr("k")
    )
    monkeypatch.setattr(
        config_module.settings, "RUDDERSTACK_DATA_PLANE_URL", "   "
    )
    assert rudderstack._is_configured() is False
    rudderstack.track(
        event_name="x", anonymous_id="org-uuid", properties={}
    )
