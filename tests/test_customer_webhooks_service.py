"""Pure-logic tests for app.services.customer_webhooks."""

from __future__ import annotations

from app.services.customer_webhooks import _filter_matches


def test_filter_matches_wildcard():
    assert _filter_matches(["*"], "page.viewed") is True


def test_filter_matches_exact():
    assert _filter_matches(["page.viewed"], "page.viewed") is True


def test_filter_matches_one_of_many():
    assert _filter_matches(["a", "b", "page.viewed"], "page.viewed") is True


def test_filter_does_not_match_when_absent():
    assert _filter_matches(["page.viewed"], "step.completed") is False


def test_filter_does_not_match_partial_prefix():
    # V1 supports literal exact match + `*`. Hierarchical wildcard
    # (e.g. ``page.*``) is intentionally NOT supported.
    assert _filter_matches(["page."], "page.viewed") is False
    assert _filter_matches(["page.*"], "page.viewed") is False


def test_filter_empty_never_matches():
    assert _filter_matches([], "anything") is False
