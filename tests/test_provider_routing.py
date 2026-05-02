"""Provider routing layer tests.

Checks #5, #7, and #8:
  - Check #5: Routing layer prefers PostGrid for supported families; Lob
    for lob-only families.
  - Check #7: Provider attribution — provider, provider_piece_id, resource_family,
    routing_decision are returned on every dispatch.
  - Check #8: snap_pack and booklet refuse PostGrid routing with a clear error.
"""

from __future__ import annotations

import pytest

from app.providers.routing.direct_mail import (
    LOB_ONLY_FAMILIES,
    POSTGRID_SUPPORTED_FAMILIES,
    DirectMailRoutingError,
    RoutingResult,
    resolve_provider,
)


# ---------------------------------------------------------------------------
# Check #8: explicit snap_pack/booklet refusal with clear error
# ---------------------------------------------------------------------------


def test_snap_pack_routes_to_lob_not_postgrid(monkeypatch):
    """snap_pack must resolve to lob-only-resource, NOT postgrid."""
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key")
    result = resolve_provider(resource_family="snap_pack", test_mode=True)
    assert result.provider == "lob"
    assert result.routing_decision == "lob-only-resource"


def test_booklet_routes_to_lob_not_postgrid(monkeypatch):
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key")
    result = resolve_provider(resource_family="booklet", test_mode=True)
    assert result.provider == "lob"
    assert result.routing_decision == "lob-only-resource"


def test_snap_pack_raises_if_no_lob_key(monkeypatch):
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: None)
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_live_key", lambda: None)
    with pytest.raises(DirectMailRoutingError, match="Lob-only"):
        resolve_provider(resource_family="snap_pack", test_mode=True)


# ---------------------------------------------------------------------------
# Check #5: PostGrid preferred for supported families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family",
    ["letter", "postcard", "cheque", "self_mailer", "template", "contact"],
)
def test_preferred_postgrid_for_supported_families(monkeypatch, family):
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._postgrid_test_key", lambda: "test_pg_key"
    )
    result = resolve_provider(resource_family=family, test_mode=True)
    assert result.provider == "postgrid"
    assert result.routing_decision == "preferred-postgrid-used"
    assert result.resource_family == family


def test_falls_back_to_lob_when_postgrid_key_missing(monkeypatch):
    """If PostGrid key not configured, fall back to Lob for supported families."""
    monkeypatch.setattr("app.providers.routing.direct_mail._postgrid_test_key", lambda: None)
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key")
    result = resolve_provider(resource_family="letter", test_mode=True)
    assert result.provider == "lob"
    assert result.routing_decision == "routing-layer-default"


def test_no_keys_raises(monkeypatch):
    monkeypatch.setattr("app.providers.routing.direct_mail._postgrid_test_key", lambda: None)
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: None)
    with pytest.raises(DirectMailRoutingError, match="No API key"):
        resolve_provider(resource_family="letter", test_mode=True)


# ---------------------------------------------------------------------------
# Check #7: Provider attribution — RoutingResult has required fields
# ---------------------------------------------------------------------------


def test_routing_result_has_attribution_fields(monkeypatch):
    """RoutingResult must carry provider, routing_decision, resource_family, api_key."""
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._postgrid_test_key", lambda: "test_pg_key"
    )
    result = resolve_provider(resource_family="letter", test_mode=True)
    assert isinstance(result, RoutingResult)
    assert result.provider in ("postgrid", "lob")
    assert result.routing_decision in (
        "preferred-postgrid-used",
        "lob-only-resource",
        "routing-layer-default",
    )
    assert result.resource_family is not None
    assert result.provider_api_key is not None


def test_lob_routing_result_attribution(monkeypatch):
    monkeypatch.setattr("app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key")
    result = resolve_provider(resource_family="snap_pack", test_mode=True)
    assert result.provider == "lob"
    assert result.routing_decision == "lob-only-resource"
    assert result.resource_family == "snap_pack"
    assert result.provider_api_key == "test_lob_key"


# ---------------------------------------------------------------------------
# Family set membership checks
# ---------------------------------------------------------------------------


def test_postgrid_supported_families_includes_required():
    required = {"letter", "postcard", "cheque", "self_mailer", "template", "contact", "return_envelope"}
    assert required.issubset(POSTGRID_SUPPORTED_FAMILIES)


def test_lob_only_families_includes_snap_pack_and_booklet():
    assert "snap_pack" in LOB_ONLY_FAMILIES
    assert "booklet" in LOB_ONLY_FAMILIES


def test_lob_only_families_not_in_postgrid_supported():
    """Lob-only families must NOT appear in the PostGrid-supported set."""
    overlap = LOB_ONLY_FAMILIES & POSTGRID_SUPPORTED_FAMILIES
    assert len(overlap) == 0, f"Unexpected overlap: {overlap}"


# ---------------------------------------------------------------------------
# Live key routing (production mode)
# ---------------------------------------------------------------------------


def test_live_mode_uses_live_keys(monkeypatch):
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._postgrid_live_key", lambda: "live_pg_key"
    )
    result = resolve_provider(resource_family="letter", test_mode=False)
    assert result.provider == "postgrid"
    assert result.provider_api_key == "live_pg_key"
