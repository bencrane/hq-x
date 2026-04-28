"""Pure-function tests for voice_campaign_batch helpers."""

from datetime import datetime, time, timezone

from app.services.voice_campaign_batch import is_within_call_window


def test_within_window_no_bounds_returns_true() -> None:
    assert is_within_call_window({}) is True
    assert is_within_call_window({"call_window_start": None}) is True


def test_within_window_business_hours_eastern() -> None:
    # 18:00 UTC = 14:00 America/New_York (EDT in summer; pick a date the
    # zoneinfo always renders correctly — winter EST.) 18:00 UTC =
    # 13:00 EST in January.
    config = {
        "call_window_start": time(9, 0),
        "call_window_end": time(17, 0),
        "call_window_timezone": "America/New_York",
    }
    inside = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    outside_early = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)  # 06:00 EST
    outside_late = datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)  # 18:00 EST

    assert is_within_call_window(config, now=inside) is True
    assert is_within_call_window(config, now=outside_early) is False
    assert is_within_call_window(config, now=outside_late) is False


def test_within_window_string_times() -> None:
    config = {
        "call_window_start": "09:00:00",
        "call_window_end": "17:00:00",
        "call_window_timezone": "America/New_York",
    }
    inside = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    assert is_within_call_window(config, now=inside) is True
