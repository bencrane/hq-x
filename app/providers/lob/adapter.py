"""Lob adapter — canonical entry point for Lob campaign-object lifecycle.

The adapter sits between ``app/services/channel_campaign_steps.py`` and the
low-level HTTP client in ``app/providers/lob/client.py``. Every step
activation / cancellation / webhook-event interpretation goes through this
class. Other parts of the codebase that talk to Lob's API
(``app/routers/direct_mail.py`` proxy endpoints, the webhook receiver) keep
calling the low-level client; the adapter is the orchestration layer that
maps **our** domain (channel_campaign_step) onto **Lob's** primitives
(campaign + creative + audience + uploads).

Tagging contract: every Lob campaign object created here carries the
six-tuple in its ``metadata`` field:

```
{
  "organization_id":         "<uuid>",
  "brand_id":                "<uuid>",
  "campaign_id":             "<uuid>",
  "channel_campaign_id":     "<uuid>",
  "channel_campaign_step_id":"<uuid>"
}
```

so webhook ingestion can resolve back to internal entities even if the
``direct_mail_pieces`` row is not yet written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models.campaigns import (
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
    ChannelCampaignStepStatus,
)
from app.dmaas.step_link_minting import (
    StepLinkMintingError,
    mint_links_for_step,
)
from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError

logger = logging.getLogger(__name__)


# ── Result envelopes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class LobActivationResult:
    """What activate_step returns. Persisted onto the step row by the
    caller."""
    status: ChannelCampaignStepStatus
    external_provider_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LobSendResult:
    """Outcome of execute_send (Lob 'send campaign' endpoint)."""
    status: ChannelCampaignStepStatus
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LobCancelResult:
    """Outcome of cancel_step. Lob does not always allow cancellation —
    callers must treat this as best-effort and clean up local state
    regardless."""
    cancelled: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedLobEvent:
    """Output of parse_webhook_event — flattened, projector-friendly view
    of a Lob webhook payload.

    ``lob_campaign_id`` is the ``cmp_*`` id (one of ours' step's
    external_provider_id when our adapter created it).
    ``lob_piece_id`` is the per-recipient ``psc_* / ltr_* / sfm_*`` id
    used to look up direct_mail_pieces.
    """
    event_type: str
    lob_campaign_id: str | None
    lob_piece_id: str | None
    occurred_at_raw: Any
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Adapter ───────────────────────────────────────────────────────────────


def _api_key(*, test_mode: bool) -> str:
    if test_mode:
        key = settings.LOB_API_KEY_TEST
        if not key:
            raise LobProviderError("LOB_API_KEY_TEST not set")
        return key
    key = settings.LOB_API_KEY
    if not key:
        raise LobProviderError("LOB_API_KEY not set")
    return key


def _step_metadata_tags(
    *, step: ChannelCampaignStepResponse
) -> dict[str, str]:
    """The six-tuple attached to every Lob object we create."""
    return {
        "organization_id": str(step.organization_id),
        "brand_id": str(step.brand_id),
        "campaign_id": str(step.campaign_id),
        "channel_campaign_id": str(step.channel_campaign_id),
        "channel_campaign_step_id": str(step.id),
    }


class LobAdapter:
    """Single entry point for our Lob campaign-object lifecycle."""

    def __init__(self, *, test_mode: bool | None = None) -> None:
        # When test_mode is None we let the per-call ``test_mode`` flag on
        # the step (via channel_specific_config) decide. Defaulting to False
        # here matches operator-driven sends, which use the live key.
        self._explicit_test_mode = test_mode

    def _resolve_test_mode(
        self, step: ChannelCampaignStepResponse
    ) -> bool:
        if self._explicit_test_mode is not None:
            return self._explicit_test_mode
        return bool(step.channel_specific_config.get("test_mode", False))

    # ── activate ─────────────────────────────────────────────────────────

    async def activate_step(
        self,
        *,
        step: ChannelCampaignStepResponse,
        channel_campaign: ChannelCampaignResponse,
    ) -> LobActivationResult:
        """Translate a step into a Lob campaign object.

        Steps:
          1. POST /v1/campaigns with metadata-tagged payload.
          2. (Future PR) Upload creative referenced by step.creative_ref.
          3. (Future PR) Upload audience CSV via /v1/uploads.
          4. Return the lob_campaign_id so the caller can persist it.

        Per the directive's non-goals, this PR scaffolds the adapter and
        wires it for steps 1 + 4 only. Creative + audience upload land
        with the multi-step scheduler in a follow-up PR.
        """
        if channel_campaign.channel != "direct_mail":
            raise LobProviderError(
                f"LobAdapter only handles channel='direct_mail' (got "
                f"{channel_campaign.channel})"
            )
        if step.creative_ref is None:
            raise LobProviderError(
                f"step {step.id} has no creative_ref — direct_mail steps "
                "require a design"
            )

        test_mode = self._resolve_test_mode(step)
        api_key = _api_key(test_mode=test_mode)

        # Mint one Dub link per recipient *before* asking Lob to create the
        # campaign object. Fail-closed: if any mint fails, we never call
        # Lob, so no print job is queued with a broken QR destination.
        destination_url = step.channel_specific_config.get("landing_page_url")
        if not destination_url:
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "channel_specific_config.landing_page_url is "
                    "required for direct_mail steps",
                },
            )

        try:
            await mint_links_for_step(
                channel_campaign_step_id=step.id,
                organization_id=step.organization_id,
                brand_id=step.brand_id,
                campaign_id=step.campaign_id,
                channel_campaign_id=step.channel_campaign_id,
                destination_url=destination_url,
            )
        except StepLinkMintingError as exc:
            logger.exception(
                "lob_adapter activate_step dub mint failed for step=%s recipient=%s",
                step.id,
                exc.recipient_id,
            )
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "dub_mint_failed",
                    "message": str(exc)[:300],
                    "recipient_id": (
                        str(exc.recipient_id) if exc.recipient_id else None
                    ),
                },
            )

        payload: dict[str, Any] = {
            "name": step.name or f"step-{step.step_order}",
            "description": (
                f"channel_campaign_step_id={step.id} step_order={step.step_order}"
            ),
            "metadata": _step_metadata_tags(step=step),
        }
        # Operator-supplied overrides (schedule_date, etc.) come from the
        # step's channel_specific_config. Whitelist what we forward to Lob;
        # don't blindly splat random keys.
        for k in ("schedule_date", "use_type", "billing_group_id"):
            v = step.channel_specific_config.get(k)
            if v is not None:
                payload[k] = v

        try:
            response = lob_client.create_campaign(api_key, payload)
        except LobProviderError as exc:
            logger.exception("lob_adapter activate_step create_campaign failed")
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={"error": str(exc)[:300]},
            )

        lob_campaign_id = response.get("id")
        if not isinstance(lob_campaign_id, str):
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "lob create_campaign returned no id",
                    "response_keys": sorted(response.keys()),
                },
            )

        return LobActivationResult(
            status="scheduled",
            external_provider_id=lob_campaign_id,
            metadata=response,
        )

    # ── execute_send / cancel ────────────────────────────────────────────

    async def execute_send(
        self, *, step: ChannelCampaignStepResponse
    ) -> LobSendResult:
        if not step.external_provider_id:
            raise LobProviderError(
                f"step {step.id} has no external_provider_id; activate first"
            )
        test_mode = self._resolve_test_mode(step)
        api_key = _api_key(test_mode=test_mode)
        try:
            response = lob_client.send_campaign(
                api_key, step.external_provider_id
            )
        except LobProviderError as exc:
            logger.exception("lob_adapter execute_send failed")
            return LobSendResult(
                status="failed",
                metadata={"error": str(exc)[:300]},
            )
        # Lob accepted the send order. Delivery state comes via webhooks;
        # we mark the step 'sent' here in the local-state sense ("we asked
        # Lob to mail it"), which the projector then refines.
        return LobSendResult(status="sent", metadata=response)

    async def cancel_step(
        self, *, step: ChannelCampaignStepResponse
    ) -> LobCancelResult:
        if not step.external_provider_id:
            return LobCancelResult(cancelled=False, metadata={"reason": "no_external_id"})
        test_mode = self._resolve_test_mode(step)
        api_key = _api_key(test_mode=test_mode)
        try:
            response = lob_client.delete_campaign(api_key, step.external_provider_id)
        except LobProviderError as exc:
            logger.warning("lob_adapter cancel_step failed: %s", exc)
            return LobCancelResult(cancelled=False, metadata={"error": str(exc)[:300]})
        return LobCancelResult(cancelled=True, metadata=response)

    # ── webhook parsing ──────────────────────────────────────────────────

    @staticmethod
    def parse_webhook_event(payload: dict[str, Any]) -> ParsedLobEvent:
        """Flatten a Lob webhook payload to (event_type, ids, when).

        Lob webhooks vary by event type; the body field that carries the
        Lob campaign / piece id is consistent enough that we can pull both
        in one pass and let the projector decide which lookup to run.
        """
        from app.webhooks.lob_normalization import (
            extract_lob_event_name,
            extract_lob_piece_id,
            normalize_lob_event_type,
        )

        event_name = extract_lob_event_name(payload)
        normalized = normalize_lob_event_type(event_name)
        piece_id = extract_lob_piece_id(payload)

        # Lob includes the campaign id on campaign-scoped events under the
        # ``body.campaign`` key; many piece events also reference it. Try a
        # few canonical paths.
        body = payload.get("body") or {}
        lob_campaign_id: str | None = None
        for candidate in (
            body.get("campaign") if isinstance(body, dict) else None,
            payload.get("campaign"),
            body.get("campaign_id") if isinstance(body, dict) else None,
            payload.get("campaign_id"),
        ):
            if isinstance(candidate, str):
                lob_campaign_id = candidate
                break

        return ParsedLobEvent(
            event_type=normalized,
            lob_campaign_id=lob_campaign_id,
            lob_piece_id=str(piece_id) if piece_id else None,
            occurred_at_raw=(
                payload.get("date_created")
                or payload.get("created_at")
                or payload.get("time")
            ),
            metadata={
                "raw_event_name": event_name,
            },
        )


__all__ = [
    "LobActivationResult",
    "LobSendResult",
    "LobCancelResult",
    "ParsedLobEvent",
    "LobAdapter",
]
