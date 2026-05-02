"""PostGrid Print & Mail adapter.

Thin orchestration layer between service-layer code and the low-level HTTP
client in app/providers/postgrid/client.py. Mirrors the LobAdapter structure
in app/providers/lob/adapter.py.

For the per-recipient per-piece activation path (the non-campaign path used by
print_mail_activation.py), this adapter exposes a create_piece method that:
  1. Resolves the correct API key (test vs live)
  2. Dispatches to the appropriate PostGrid endpoint based on piece_type
  3. Returns a normalized response dict with provider attribution

Provider attribution fields on every response:
  - provider         : 'postgrid'
  - provider_piece_id: the PostGrid resource id (letter_*, postcard_*, etc.)
  - resource_family  : e.g. 'letter'
  - routing_decision : 'preferred-postgrid-used'
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.providers.postgrid import client as pg_client
from app.providers.postgrid.client import PostGridProviderError

logger = logging.getLogger(__name__)


def _api_key(*, test_mode: bool) -> str:
    if test_mode:
        key = settings.POSTGRID_PRINT_MAIL_API_KEY_TEST
        if not key:
            raise PostGridProviderError("POSTGRID_PRINT_MAIL_API_KEY_TEST not set")
        return key
    key = settings.POSTGRID_PRINT_MAIL_API_KEY_LIVE
    if not key:
        raise PostGridProviderError("POSTGRID_PRINT_MAIL_API_KEY_LIVE not set")
    return key


@dataclass(frozen=True)
class PostGridPieceResult:
    """Outcome of a PostGrid piece create."""

    provider: str = "postgrid"
    provider_piece_id: str | None = None
    resource_family: str | None = None
    routing_decision: str = "preferred-postgrid-used"
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    success: bool = True


class PostGridAdapter:
    """Entry point for PostGrid per-piece direct mail dispatch."""

    def __init__(self, *, test_mode: bool = False) -> None:
        self._test_mode = test_mode

    def create_piece(
        self,
        *,
        piece_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> PostGridPieceResult:
        """Create a direct-mail piece via PostGrid.

        piece_type is one of: letter, postcard, cheque, self_mailer.
        Snap-packs and booklets are not supported by PostGrid — callers
        should use the LobAdapter for those.

        Returns PostGridPieceResult with provider attribution fields set.
        """
        unsupported = {"snap_pack", "snap_packs", "booklet", "booklets"}
        if piece_type.lower() in unsupported:
            raise PostGridProviderError(
                f"PostGrid does not support piece_type={piece_type!r}. "
                "Snap-packs and booklets are Lob-only resources. "
                "Route this request to the Lob provider."
            )

        api_key = _api_key(test_mode=self._test_mode)

        try:
            if piece_type.lower() in ("letter",):
                response = pg_client.create_letter(
                    api_key, payload,
                    idempotency_key=idempotency_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                )
            elif piece_type.lower() in ("postcard",):
                response = pg_client.create_postcard(
                    api_key, payload,
                    idempotency_key=idempotency_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                )
            elif piece_type.lower() in ("cheque",):
                response = pg_client.create_cheque(
                    api_key, payload,
                    idempotency_key=idempotency_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                )
            elif piece_type.lower() in ("self_mailer", "selfmailer"):
                response = pg_client.create_self_mailer(
                    api_key, payload,
                    idempotency_key=idempotency_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                )
            else:
                raise PostGridProviderError(
                    f"Unknown piece_type={piece_type!r} for PostGrid adapter"
                )
        except PostGridProviderError as exc:
            logger.error("PostGrid create_piece failed: %s", exc)
            return PostGridPieceResult(
                success=False,
                error=str(exc)[:300],
                resource_family=piece_type,
            )

        piece_id = response.get("id")
        return PostGridPieceResult(
            provider="postgrid",
            provider_piece_id=str(piece_id) if piece_id else None,
            resource_family=piece_type,
            routing_decision="preferred-postgrid-used",
            raw_response=response,
            success=True,
        )


__all__ = [
    "PostGridAdapter",
    "PostGridPieceResult",
]
