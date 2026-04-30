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

Activation flow (Slice 1, V1: caller-supplied creative)
-------------------------------------------------------

For V1 we do not auto-render creative from ``dmaas_designs.id``. The
operator/customer prepares the creative externally (Figma → PDF, hand
HTML, etc.) and supplies it on the step:

```
step.channel_specific_config["lob_creative_payload"] = {
    "resource_type": "postcard",
    "front": "<html>...</html>" | "tmpl_..." | "https://.../front.pdf",
    "back":  "<html>...</html>" | "tmpl_..." | "https://.../back.pdf",
    "details": { "size": "4x6", ... },
    "from": "adr_..." | { ... }
}
```

``activate_step`` then runs (in order):

  1. Mint per-recipient Dub links (idempotent on step + recipient).
  2. Create the Lob campaign object (six-tuple-tagged).
  3. Create the Lob creative bound to the campaign.
  4. Build the audience CSV from step memberships + recipients +
     Dub links.
  5. Create the Lob upload row (column mappings declared up front).
  6. POST the CSV file to ``/uploads/{upl_id}/file``; Lob mints
     pieces server-side and fires per-piece webhooks tagged with the
     step's metadata.

Each external call uses a deterministic Lob idempotency key derived
from the step id, so a partial-failure retry resumes mid-flow rather
than re-creating already-existing Lob objects. The step row carries
``external_provider_id`` (= Lob campaign id) and
``external_provider_metadata.{lob_creative_id, lob_upload_id}`` so the
adapter knows which sub-steps are already complete on retry.

Renderer note
-------------

Building ``dmaas_designs → Lob creative HTML`` is a multi-PR project on
its own (HTML synthesis, CSS positioning, font/asset hosting,
panel-aware self-mailer geometry). Until that lands the operator
supplies creative directly. ``creative_ref`` (= ``dmaas_designs.id``)
is preserved on the step row as metadata for the future renderer
wiring; it is not consumed by this adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.db import get_db_connection
from app.dmaas.step_link_minting import (
    StepLinkMintingError,
    mint_links_for_step,
)
from app.models.campaigns import (
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
    ChannelCampaignStepStatus,
)
from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError
from app.services.lob_audience_csv import (
    LOB_MERGE_VARIABLE_COLUMN_MAPPING,
    LOB_OPTIONAL_COLUMN_MAPPING,
    LOB_REQUIRED_COLUMN_MAPPING,
    AudienceRow,
    AudienceRowInvalid,
    build_audience_csv,
)

# Allowed values for lob_creative_payload.resource_type. Anything else
# fails activation early with a structured error.
_ALLOWED_CREATIVE_RESOURCE_TYPES = ("postcard", "letter", "self_mailer")

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


def _validate_lob_creative_payload(payload: Any) -> str | None:
    """Return None if the payload is well-formed, else an error message.

    The schema is intentionally minimal — we forward most fields straight
    through to Lob's ``POST /v1/creatives`` and let Lob reject anything
    it doesn't like. We only check what we depend on locally:

    * the payload exists and is a dict
    * ``resource_type`` is one of postcard / letter / self_mailer
    * ``front`` and ``back`` are present (Lob requires both for the
      formats we support)
    * ``details`` is present (Lob expects it; size/orientation lives there)
    """
    if not isinstance(payload, dict):
        return (
            "channel_specific_config.lob_creative_payload is required for "
            "direct_mail steps (operator must supply caller-rendered "
            "creative until the dmaas_designs renderer ships)"
        )
    resource_type = payload.get("resource_type")
    if resource_type not in _ALLOWED_CREATIVE_RESOURCE_TYPES:
        allowed = ", ".join(_ALLOWED_CREATIVE_RESOURCE_TYPES)
        return (
            f"lob_creative_payload.resource_type must be one of {allowed}; "
            f"got {resource_type!r}"
        )
    for required in ("front", "back", "details"):
        if required not in payload or payload[required] in (None, ""):
            return (
                f"lob_creative_payload.{required} is required for "
                f"resource_type={resource_type}"
            )
    return None


async def _build_audience_rows_for_step(
    *,
    step: ChannelCampaignStepResponse,
) -> list[AudienceRow]:
    """Read pending memberships for this step, joined to recipients +
    Dub links, and project into AudienceRow instances ready for the
    CSV builder.

    The query is org-scoped via ``scr.organization_id`` (denormalized on
    every membership row) and pulls only ``status='pending'`` rows — once
    a membership transitions out of pending it has already been included
    in some prior upload.
    """
    sql = """
        SELECT
            r.display_name,
            r.mailing_address,
            dl.dub_short_url
        FROM business.channel_campaign_step_recipients scr
        JOIN business.recipients r ON r.id = scr.recipient_id
        LEFT JOIN dmaas_dub_links dl
          ON dl.channel_campaign_step_id = scr.channel_campaign_step_id
         AND dl.recipient_id = scr.recipient_id
        WHERE scr.channel_campaign_step_id = %s
          AND scr.organization_id = %s
          AND scr.status = 'pending'
        ORDER BY scr.created_at
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (str(step.id), str(step.organization_id)),
            )
            rows = await cur.fetchall()

    out: list[AudienceRow] = []
    for display_name, mailing_address, dub_short_url in rows:
        addr = mailing_address if isinstance(mailing_address, dict) else {}
        out.append(
            AudienceRow(
                recipient_name=str(display_name or "").strip(),
                primary_line=str(addr.get("line1") or "").strip(),
                secondary_line=(
                    str(addr["line2"]).strip() if addr.get("line2") else None
                ),
                city=str(addr.get("city") or "").strip(),
                state=str(addr.get("state") or "").strip(),
                zip_code=str(addr.get("zip") or "").strip(),
                country=(
                    str(addr["country"]).strip() if addr.get("country") else None
                ),
                qr_code_redirect_url=(
                    str(dub_short_url).strip() if dub_short_url else None
                ),
            )
        )
    return out


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
        """Translate a step into a Lob campaign + creative + audience.

        See module docstring for the full flow. The high-level guarantee:
        on first run we mint Dub links, create the Lob campaign + creative,
        build the audience CSV, and submit the upload. On retry (called
        with a step that already has ``external_provider_id`` and / or
        ``external_provider_metadata.{lob_creative_id,lob_upload_id}`` set),
        we skip the sub-steps that already succeeded and resume from the
        next one — same idempotency keys keep Lob's side consistent.
        """
        if channel_campaign.channel != "direct_mail":
            raise LobProviderError(
                f"LobAdapter only handles channel='direct_mail' (got "
                f"{channel_campaign.channel})"
            )
        if step.creative_ref is None:
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "missing_creative_ref",
                    "message": (
                        f"step {step.id} has no creative_ref — direct_mail "
                        "steps require a design"
                    ),
                },
            )

        # ── Pre-flight validation ─────────────────────────────────────
        destination_url = step.channel_specific_config.get("landing_page_url")
        if not destination_url:
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "missing_landing_page_url",
                    "message": (
                        "channel_specific_config.landing_page_url is "
                        "required for direct_mail steps"
                    ),
                },
            )

        creative_payload_raw = step.channel_specific_config.get(
            "lob_creative_payload"
        )
        creative_validation_error = _validate_lob_creative_payload(
            creative_payload_raw
        )
        if creative_validation_error is not None:
            return LobActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "missing_lob_creative_payload",
                    "message": creative_validation_error,
                },
            )
        # Narrowed type: validated above.
        assert isinstance(creative_payload_raw, dict)
        creative_payload: dict[str, Any] = creative_payload_raw

        test_mode = self._resolve_test_mode(step)
        api_key = _api_key(test_mode=test_mode)

        # External-provider state that may already be populated from a
        # prior partial activation. We resume from wherever we left off.
        existing_metadata: dict[str, Any] = (
            step.external_provider_metadata or {}
        )
        existing_creative_id = existing_metadata.get("lob_creative_id")
        existing_upload_id = existing_metadata.get("lob_upload_id")

        # ── Step 1 — Mint Dub links ───────────────────────────────────
        # Fail-closed: if any mint fails, we never call Lob, so no print
        # job is queued with a broken QR destination. Mint is idempotent
        # on (step, recipient) so retries are cheap.
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

        # ── Step 2 — Lob campaign object ──────────────────────────────
        if step.external_provider_id:
            lob_campaign_id = step.external_provider_id
        else:
            campaign_payload: dict[str, Any] = {
                "name": step.name or f"step-{step.step_order}",
                "description": (
                    f"channel_campaign_step_id={step.id} "
                    f"step_order={step.step_order}"
                ),
                # Lob requires ``schedule_type`` on every /v1/campaigns
                # create. Today the API only accepts "immediate" without
                # an accompanying date; operators wanting a delayed send
                # override via channel_specific_config.schedule_type +
                # send_date / target_delivery_date.
                "schedule_type": step.channel_specific_config.get(
                    "schedule_type", "immediate"
                ),
                "metadata": _step_metadata_tags(step=step),
            }
            for k in (
                "schedule_date",
                "send_date",
                "target_delivery_date",
                "use_type",
                "billing_group_id",
                "cancel_window_campaign_minutes",
            ):
                v = step.channel_specific_config.get(k)
                if v is not None:
                    campaign_payload[k] = v

            try:
                campaign_response = lob_client.create_campaign(
                    api_key,
                    campaign_payload,
                    idempotency_key=f"hqx-step-{step.id}-campaign",
                )
            except LobProviderError as exc:
                logger.exception(
                    "lob_adapter activate_step create_campaign failed"
                )
                return LobActivationResult(
                    status="failed",
                    external_provider_id=None,
                    metadata={
                        "error": "lob_campaign_create_failed",
                        "message": str(exc)[:300],
                    },
                )

            campaign_id_raw = campaign_response.get("id")
            if not isinstance(campaign_id_raw, str):
                return LobActivationResult(
                    status="failed",
                    external_provider_id=None,
                    metadata={
                        "error": "lob_campaign_create_no_id",
                        "response_keys": sorted(campaign_response.keys()),
                    },
                )
            lob_campaign_id = campaign_id_raw

        # ── Step 3 — Lob creative ─────────────────────────────────────
        if existing_creative_id:
            lob_creative_id: str | None = existing_creative_id
        else:
            creative_request = {
                **creative_payload,
                "campaign_id": lob_campaign_id,
                "metadata": _step_metadata_tags(step=step),
            }
            try:
                creative_response = lob_client.create_creative(
                    api_key,
                    creative_request,
                    idempotency_key=f"hqx-step-{step.id}-creative",
                )
            except LobProviderError as exc:
                logger.exception(
                    "lob_adapter activate_step create_creative failed"
                )
                return LobActivationResult(
                    status="activating",
                    external_provider_id=lob_campaign_id,
                    metadata={
                        "error": "lob_creative_create_failed",
                        "message": str(exc)[:300],
                    },
                )
            creative_id_raw = creative_response.get("id")
            if not isinstance(creative_id_raw, str):
                return LobActivationResult(
                    status="activating",
                    external_provider_id=lob_campaign_id,
                    metadata={
                        "error": "lob_creative_create_no_id",
                        "response_keys": sorted(creative_response.keys()),
                    },
                )
            lob_creative_id = creative_id_raw

        # ── Step 4 — Build audience CSV ───────────────────────────────
        audience_rows = await _build_audience_rows_for_step(step=step)
        if not audience_rows:
            return LobActivationResult(
                status="activating",
                external_provider_id=lob_campaign_id,
                metadata={
                    "error": "audience_empty",
                    "message": (
                        "step has no pending memberships — materialize the "
                        "audience before activating"
                    ),
                    "lob_creative_id": lob_creative_id,
                },
            )
        try:
            csv_bytes = build_audience_csv(audience_rows)
        except AudienceRowInvalid as exc:
            return LobActivationResult(
                status="activating",
                external_provider_id=lob_campaign_id,
                metadata={
                    "error": "audience_row_invalid",
                    "message": str(exc)[:300],
                    "lob_creative_id": lob_creative_id,
                },
            )

        # ── Step 5 — Lob upload row ───────────────────────────────────
        if existing_upload_id:
            lob_upload_id: str | None = existing_upload_id
        else:
            upload_request = {
                "campaignId": lob_campaign_id,
                "requiredAddressColumnMapping": LOB_REQUIRED_COLUMN_MAPPING,
                "optionalAddressColumnMapping": LOB_OPTIONAL_COLUMN_MAPPING,
                "mergeVariableColumnMapping": (
                    LOB_MERGE_VARIABLE_COLUMN_MAPPING
                ),
            }
            try:
                upload_response = lob_client.create_upload(
                    api_key,
                    upload_request,
                )
            except LobProviderError as exc:
                logger.exception(
                    "lob_adapter activate_step create_upload failed"
                )
                return LobActivationResult(
                    status="activating",
                    external_provider_id=lob_campaign_id,
                    metadata={
                        "error": "lob_upload_create_failed",
                        "message": str(exc)[:300],
                        "lob_creative_id": lob_creative_id,
                    },
                )
            upload_id_raw = upload_response.get("id")
            if not isinstance(upload_id_raw, str):
                return LobActivationResult(
                    status="activating",
                    external_provider_id=lob_campaign_id,
                    metadata={
                        "error": "lob_upload_create_no_id",
                        "response_keys": sorted(upload_response.keys()),
                        "lob_creative_id": lob_creative_id,
                    },
                )
            lob_upload_id = upload_id_raw

        # ── Step 6 — POST CSV file ────────────────────────────────────
        try:
            file_response = lob_client.upload_file(
                api_key,
                lob_upload_id,
                file_name=f"step-{step.id}-audience.csv",
                file_content=csv_bytes,
                content_type="text/csv",
            )
        except LobProviderError as exc:
            logger.exception("lob_adapter activate_step upload_file failed")
            return LobActivationResult(
                status="activating",
                external_provider_id=lob_campaign_id,
                metadata={
                    "error": "lob_upload_file_failed",
                    "message": str(exc)[:300],
                    "lob_creative_id": lob_creative_id,
                    "lob_upload_id": lob_upload_id,
                },
            )

        return LobActivationResult(
            status="scheduled",
            external_provider_id=lob_campaign_id,
            metadata={
                "lob_creative_id": lob_creative_id,
                "lob_upload_id": lob_upload_id,
                "lob_upload_file_response": file_response,
                "audience_size": len(audience_rows),
            },
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
