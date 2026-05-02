"""Direct-mail provider routing layer.

Prefers PostGrid for the 7 resource families it supports:
  letters, postcards, cheques, self_mailers, return_envelopes, templates, contacts

Lob is the ONLY provider for:
  snap_packs, booklets (no PostGrid analog — hard error if PostGrid routing attempted)

Routing decisions:
  - 'preferred-postgrid-used'  : PostGrid was preferred and available
  - 'lob-only-resource'        : resource family has no PostGrid analog
  - 'routing-layer-default'    : PostGrid unavailable/not configured; Lob used as fallback

Provider attribution is attached to every dispatch result. The caller
is responsible for persisting these fields on direct_mail_pieces:
  provider         : 'postgrid' | 'lob'
  provider_piece_id: the provider's resource id
  resource_family  : e.g. 'letter', 'postcard'
  routing_decision : one of the three values above
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.config import settings

# Resource families PostGrid supports (and we prefer it for)
POSTGRID_SUPPORTED_FAMILIES = frozenset(
    {
        "letter",
        "letters",
        "postcard",
        "postcards",
        "cheque",
        "cheques",
        "self_mailer",
        "self_mailers",
        "selfmailer",
        "selfmailers",
        "return_envelope",
        "return_envelopes",
        "template",
        "templates",
        "contact",
        "contacts",
    }
)

# Resource families with NO PostGrid analog — must stay on Lob
LOB_ONLY_FAMILIES = frozenset(
    {
        "snap_pack",
        "snap_packs",
        "booklet",
        "booklets",
    }
)

RoutingDecision = Literal[
    "preferred-postgrid-used",
    "lob-only-resource",
    "routing-layer-default",
]

Provider = Literal["postgrid", "lob"]


@dataclass(frozen=True)
class RoutingResult:
    """Result of a routing decision. Attach to every dispatch for attribution."""

    provider: Provider
    routing_decision: RoutingDecision
    resource_family: str
    provider_api_key: str


class DirectMailRoutingError(Exception):
    """Raised when routing is attempted for an unsupported combination."""


def _postgrid_test_key() -> str | None:
    return getattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_TEST", None)


def _postgrid_live_key() -> str | None:
    return getattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_LIVE", None)


def _lob_test_key() -> str | None:
    return settings.LOB_API_KEY_TEST


def _lob_live_key() -> str | None:
    return settings.LOB_API_KEY


def resolve_provider(
    *,
    resource_family: str,
    test_mode: bool = False,
) -> RoutingResult:
    """Decide which provider to use for this dispatch.

    Args:
        resource_family: the piece type (e.g. 'letter', 'snap_pack').
        test_mode: True → use test-mode keys; False → use live keys.

    Returns:
        RoutingResult with provider, routing_decision, and api_key.

    Raises:
        DirectMailRoutingError: if resource_family is a Lob-only type and
            PostGrid routing was explicitly requested, or if no configured
            key is available for the resolved provider.
    """
    normalized = resource_family.lower().replace("-", "_")

    # 1. Hard-refuse PostGrid for Lob-only resources
    if normalized in LOB_ONLY_FAMILIES:
        key = _lob_test_key() if test_mode else _lob_live_key()
        if not key:
            raise DirectMailRoutingError(
                f"resource_family={resource_family!r} is Lob-only (no PostGrid analog) "
                "but Lob API key is not configured"
            )
        return RoutingResult(
            provider="lob",
            routing_decision="lob-only-resource",
            resource_family=normalized,
            provider_api_key=key,
        )

    # 2. For PostGrid-supported families, try PostGrid first
    if normalized in POSTGRID_SUPPORTED_FAMILIES:
        pg_key = _postgrid_test_key() if test_mode else _postgrid_live_key()
        if pg_key:
            return RoutingResult(
                provider="postgrid",
                routing_decision="preferred-postgrid-used",
                resource_family=normalized,
                provider_api_key=pg_key,
            )
        # PostGrid key not configured — fall through to Lob
        lob_key = _lob_test_key() if test_mode else _lob_live_key()
        if not lob_key:
            raise DirectMailRoutingError(
                f"No API key configured for any provider "
                f"(resource_family={resource_family!r}, test_mode={test_mode})"
            )
        return RoutingResult(
            provider="lob",
            routing_decision="routing-layer-default",
            resource_family=normalized,
            provider_api_key=lob_key,
        )

    # 3. Unknown resource family — default to Lob
    lob_key = _lob_test_key() if test_mode else _lob_live_key()
    if not lob_key:
        raise DirectMailRoutingError(
            f"Unknown resource_family={resource_family!r} and Lob key not configured"
        )
    return RoutingResult(
        provider="lob",
        routing_decision="routing-layer-default",
        resource_family=normalized,
        provider_api_key=lob_key,
    )


def dispatch_piece(
    *,
    resource_family: str,
    payload: dict[str, Any],
    test_mode: bool = False,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 12.0,
) -> tuple[dict[str, Any], RoutingResult]:
    """Dispatch a direct-mail piece through the routing layer.

    Returns (provider_response, routing_result). The caller persists
    routing_result.provider, routing_result.provider_piece_id (= response["id"]),
    routing_result.resource_family, and routing_result.routing_decision onto
    the direct_mail_pieces row.

    Raises:
        DirectMailRoutingError: if resource_family is Lob-only and was
            explicitly routed to PostGrid, or if no keys are configured.
        PostGridProviderError / LobProviderError: propagated from the
            underlying client.
    """
    routing = resolve_provider(resource_family=resource_family, test_mode=test_mode)

    if routing.provider == "postgrid":
        from app.providers.postgrid import client as pg_client

        normalized = routing.resource_family
        if normalized in ("letter", "letters"):
            response = pg_client.create_letter(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("postcard", "postcards"):
            response = pg_client.create_postcard(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("cheque", "cheques"):
            response = pg_client.create_cheque(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("self_mailer", "self_mailers", "selfmailer", "selfmailers"):
            response = pg_client.create_self_mailer(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("template", "templates"):
            response = pg_client.create_template(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("contact", "contacts"):
            response = pg_client.create_contact(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        else:
            # return_envelope — PostGrid treats this as a letter flag
            response = pg_client.create_letter(
                routing.provider_api_key,
                {**payload, "returnEnvelope": True},
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
    else:
        # Lob routing
        from app.providers.lob import client as lob_client

        normalized = routing.resource_family
        if normalized in ("letter", "letters"):
            response = lob_client.create_letter(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("postcard", "postcards"):
            response = lob_client.create_postcard(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("self_mailer", "self_mailers"):
            response = lob_client.create_self_mailer(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("snap_pack", "snap_packs"):
            response = lob_client.create_snap_pack(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("booklet", "booklets"):
            response = lob_client.create_booklet(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        elif normalized in ("template", "templates"):
            response = lob_client.create_template(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        else:
            # contact → Lob address
            response = lob_client.create_address(
                routing.provider_api_key,
                payload,
                idempotency_key=idempotency_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )

    return response, routing
