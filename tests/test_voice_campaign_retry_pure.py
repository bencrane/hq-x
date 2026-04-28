"""Pure-function tests for voice_campaign_retry decision helpers."""

from app.services.voice_campaign_retry import (
    classify_outcome,
    compute_retry_delay_hours,
    get_retry_policy,
    should_retry,
)


def test_get_retry_policy_defaults_when_none() -> None:
    p = get_retry_policy(None)
    assert p["max_attempts"] == 3
    assert p["delay_hours"] == 4
    assert p["backoff_multiplier"] == 1.5


def test_get_retry_policy_merges_partial() -> None:
    p = get_retry_policy({"max_attempts": 5})
    assert p["max_attempts"] == 5
    # Defaults preserved.
    assert p["delay_hours"] == 4
    assert p["backoff_multiplier"] == 1.5


def test_should_retry_only_for_retriable_outcomes() -> None:
    assert should_retry("no_answer", attempts=0) is True
    assert should_retry("busy", attempts=2) is True
    assert should_retry("error", attempts=2) is True
    assert should_retry("transferred", attempts=0) is False
    assert should_retry("not_qualified", attempts=0) is False


def test_should_retry_respects_max_attempts() -> None:
    assert should_retry("no_answer", attempts=3) is False  # default max=3
    assert should_retry("no_answer", attempts=2) is True


def test_compute_retry_delay_busy_halves() -> None:
    delay = compute_retry_delay_hours("busy", attempts=0)
    # default delay_hours is 4 → 4/2 = 2
    assert delay == 2.0


def test_compute_retry_delay_busy_floor_one_hour() -> None:
    delay = compute_retry_delay_hours("busy", attempts=0, retry_policy={"delay_hours": 1})
    assert delay == 1.0


def test_compute_retry_delay_no_answer_flat() -> None:
    delay = compute_retry_delay_hours("no_answer", attempts=2)
    assert delay == 4.0


def test_compute_retry_delay_error_exponential() -> None:
    # default delay 4, multiplier 1.5
    assert compute_retry_delay_hours("error", attempts=0) == 4.0
    assert compute_retry_delay_hours("error", attempts=1) == 6.0
    assert compute_retry_delay_hours("error", attempts=2) == 9.0


def test_classify_outcome_buckets() -> None:
    assert classify_outcome("transferred") == "success"
    assert classify_outcome("voicemail_left") == "success"
    assert classify_outcome("callback_requested") == "success"
    assert classify_outcome("no_answer") == "retry"
    assert classify_outcome("busy") == "retry"
    assert classify_outcome("error") == "retry"
    assert classify_outcome("not_qualified") == "terminal_failure"
    assert classify_outcome("anything_else") == "unknown"
