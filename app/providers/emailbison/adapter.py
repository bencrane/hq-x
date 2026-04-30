"""EmailBison adapter — canonical entry point for the email channel.

Sits between ``app/services/channel_campaign_steps.py`` and the low-level
HTTP client in ``app/providers/emailbison/client.py``. Mirrors the shape
of ``app/providers/lob/adapter.py``: dataclass result envelopes, the
``activate_step / execute_send / cancel_step / parse_webhook_event``
method set, and the failed-result-on-provider-error pattern.

Tagging contract: EmailBison has no metadata field on Campaign (verified
against ``docs/emailbison-api-mcp-coverage.md`` §1 / §6). We encode the
six-tuple as five tag strings on the EB campaign:

    hqx:org=<uuid>
    hqx:brand=<uuid>
    hqx:campaign=<uuid>
    hqx:cc=<uuid>
    hqx:step=<uuid>

Tag attachment is best-effort. The primary path for resolving a webhook
back to a step is ``step.external_provider_id`` (= the EB campaign id).
The hqx:* tags are the secondary fallback for orphan recovery.
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
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError

logger = logging.getLogger(__name__)


_HQX_TAG_PREFIX = "hqx:"


# ── Result envelopes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmailBisonActivationResult:
    status: ChannelCampaignStepStatus
    external_provider_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailBisonSendResult:
    status: ChannelCampaignStepStatus
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailBisonCancelResult:
    cancelled: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedEmailBisonEvent:
    event_type: str
    raw_event_name: str
    eb_workspace_id: str | None
    eb_campaign_id: int | None
    eb_scheduled_email_id: int | None
    eb_lead_id: int | None
    eb_sender_email_id: int | None
    eb_reply_id: int | None
    occurred_at_raw: Any
    six_tuple_tags: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Tag helpers ───────────────────────────────────────────────────────────


def _step_tag_strings(*, step: ChannelCampaignStepResponse) -> list[str]:
    return [
        f"hqx:org={step.organization_id}",
        f"hqx:brand={step.brand_id}",
        f"hqx:campaign={step.campaign_id}",
        f"hqx:cc={step.channel_campaign_id}",
        f"hqx:step={step.id}",
    ]


_TAG_AXIS_KEYS = {
    "org": "organization_id",
    "brand": "brand_id",
    "campaign": "campaign_id",
    "cc": "channel_campaign_id",
    "step": "channel_campaign_step_id",
}


def _parse_six_tuple_from_tags(
    tags: list[dict[str, Any]] | list[str] | None,
) -> dict[str, str]:
    """Pull the hqx:* tags out of an EB campaign tag array.

    Tags arrive in webhooks as ``list[{"id":..., "name":"hqx:step=..."}]``;
    fall back to ``list[str]`` for hand-built test payloads.
    """
    if not tags:
        return {}
    out: dict[str, str] = {}
    for raw in tags:
        if isinstance(raw, dict):
            name = raw.get("name")
        elif isinstance(raw, str):
            name = raw
        else:
            continue
        if not isinstance(name, str) or not name.startswith(_HQX_TAG_PREFIX):
            continue
        body = name[len(_HQX_TAG_PREFIX):]
        if "=" not in body:
            continue
        axis, _, value = body.partition("=")
        out_key = _TAG_AXIS_KEYS.get(axis)
        if out_key:
            out[axis] = value
            out[out_key] = value
    return out


# ── Event-type normalization (coverage doc §5) ────────────────────────────


_EVENT_TYPE_NORMALIZATION = {
    "email_sent": "sent",
    "manual_email_sent": "manual_sent",
    "lead_first_contacted": "first_contacted",
    "lead_replied": "replied",
    "lead_interested": "interested",
    "lead_unsubscribed": "unsubscribed",
    "untracked_reply_received": "untracked_reply",
    "email_opened": "opened",
    "email_bounced": "bounced",
    "email_account_added": "email_account_added",
    "email_account_removed": "email_account_removed",
    "email_account_disconnected": "email_account_disconnected",
    "email_account_reconnected": "email_account_reconnected",
    "tag_attached": "tag_attached",
    "tag_removed": "tag_removed",
    "warmup_disabled_receiving_bounces": "warmup_disabled_receiving_bounces",
    "warmup_disabled_causing_bounces": "warmup_disabled_causing_bounces",
}


def _normalize_event_type(raw: str) -> str:
    if not raw:
        return "unknown"
    key = raw.lower().strip()
    return _EVENT_TYPE_NORMALIZATION.get(key, key)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


# ── Adapter ───────────────────────────────────────────────────────────────


class EmailBisonAdapter:
    """Single entry point for our EmailBison campaign-object lifecycle."""

    def __init__(self, *, api_key: str | None = None) -> None:
        self._explicit_api_key = api_key

    def _api_key(self) -> str:
        key = self._explicit_api_key or settings.EMAILBISON_API_KEY
        if not key:
            raise EmailBisonProviderError("EMAILBISON_API_KEY not set")
        return key

    # ── activate ─────────────────────────────────────────────────────────

    async def activate_step(
        self,
        *,
        step: ChannelCampaignStepResponse,
        channel_campaign: ChannelCampaignResponse,
    ) -> EmailBisonActivationResult:
        """Translate a step into an EB campaign object.

        1. POST /api/campaigns with {"name": step.name, "type": ...}.
        2. Best-effort: attach the six hqx:* tags via /api/tags/attach-to-campaigns.
           On any tag failure log a warning; do not fail activation.
        3. If channel_specific_config.sender_email_ids: attach via
           /api/campaigns/{id}/attach-sender-emails.
        4. Return EmailBisonActivationResult(status='scheduled',
           external_provider_id=str(eb_campaign_id), metadata=full_response).
        """
        if channel_campaign.channel != "email":
            raise EmailBisonProviderError(
                f"EmailBisonAdapter only handles channel='email' (got "
                f"{channel_campaign.channel})"
            )

        api_key = self._api_key()

        campaign_type = step.channel_specific_config.get("campaign_type") or "sequence"
        name = step.name or f"step-{step.step_order}"
        payload: dict[str, Any] = {
            "name": name,
            "type": campaign_type,
        }
        for key in ("max_emails_per_day", "description"):
            value = step.channel_specific_config.get(key)
            if value is not None:
                payload[key] = value

        try:
            response = eb_client.create_campaign(api_key, payload)
        except EmailBisonProviderError as exc:
            logger.exception("emailbison_adapter activate_step create_campaign failed")
            return EmailBisonActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={"error": str(exc)[:300]},
            )

        eb_campaign_id = (
            response.get("id")
            if isinstance(response, dict)
            else None
        )
        if eb_campaign_id is None and isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                eb_campaign_id = data.get("id")
        if eb_campaign_id is None:
            return EmailBisonActivationResult(
                status="failed",
                external_provider_id=None,
                metadata={
                    "error": "emailbison create_campaign returned no id",
                    "response_keys": (
                        sorted(response.keys()) if isinstance(response, dict) else []
                    ),
                },
            )
        external_provider_id = str(eb_campaign_id)

        tag_attempt: dict[str, Any] = {
            "tags": _step_tag_strings(step=step),
            "attached": False,
        }
        # Tag attachment is best-effort. Worst case we log + fall back to
        # external_provider_id resolution in the projector.
        try:
            tag_response = eb_client.attach_tags_to_campaigns(
                api_key,
                campaign_ids=[eb_campaign_id],
                tag_names=tag_attempt["tags"],
            )
            tag_attempt["attached"] = True
            tag_attempt["response"] = tag_response
        except EmailBisonProviderError as exc:
            logger.warning(
                "emailbison_adapter tag-attach failed for campaign=%s: %s",
                eb_campaign_id,
                exc,
            )
            tag_attempt["error"] = str(exc)[:300]

        sender_email_ids = step.channel_specific_config.get("sender_email_ids")
        sender_attempt: dict[str, Any] | None = None
        if isinstance(sender_email_ids, list) and sender_email_ids:
            try:
                sender_response = eb_client.attach_sender_emails(
                    api_key, eb_campaign_id, list(sender_email_ids)
                )
                sender_attempt = {
                    "attached": True,
                    "response": sender_response,
                }
            except EmailBisonProviderError as exc:
                logger.warning(
                    "emailbison_adapter sender-email attach failed for campaign=%s: %s",
                    eb_campaign_id,
                    exc,
                )
                sender_attempt = {"attached": False, "error": str(exc)[:300]}

        metadata: dict[str, Any] = {
            "create_campaign_response": response,
            "tag_attach": tag_attempt,
        }
        if sender_attempt is not None:
            metadata["sender_email_attach"] = sender_attempt

        return EmailBisonActivationResult(
            status="scheduled",
            external_provider_id=external_provider_id,
            metadata=metadata,
        )

    # ── execute_send / cancel ────────────────────────────────────────────

    async def execute_send(
        self, *, step: ChannelCampaignStepResponse
    ) -> EmailBisonSendResult:
        """Stub. Lead attachment requires recipient → eb_lead_id resolution
        which depends on a separate scoping decision (use email or external
        id as the EB natural key). Out of scope for this PR.
        """
        return EmailBisonSendResult(
            status=step.status,
            metadata={
                "note": "execute_send not yet wired; lead attach is a follow-up",
                "step_id": str(step.id),
            },
        )

    async def cancel_step(
        self, *, step: ChannelCampaignStepResponse
    ) -> EmailBisonCancelResult:
        """Pause then archive. EB has no DELETE-equivalent that's safe;
        archive is the cancellation analogue. Best-effort — return
        cancelled=False on provider error, never raise.
        """
        if not step.external_provider_id:
            return EmailBisonCancelResult(
                cancelled=False, metadata={"reason": "no_external_id"}
            )

        try:
            api_key = self._api_key()
        except EmailBisonProviderError as exc:
            return EmailBisonCancelResult(
                cancelled=False, metadata={"error": str(exc)[:300]}
            )

        eb_campaign_id = step.external_provider_id
        outcome: dict[str, Any] = {
            "pause": None,
            "archive": None,
        }
        try:
            outcome["pause"] = eb_client.pause_campaign(api_key, eb_campaign_id)
        except EmailBisonProviderError as exc:
            logger.warning(
                "emailbison_adapter cancel_step pause failed: %s", exc
            )
            outcome["pause_error"] = str(exc)[:300]

        try:
            outcome["archive"] = eb_client.archive_campaign(api_key, eb_campaign_id)
        except EmailBisonProviderError as exc:
            logger.warning(
                "emailbison_adapter cancel_step archive failed: %s", exc
            )
            outcome["archive_error"] = str(exc)[:300]

        cancelled = outcome.get("archive") is not None
        return EmailBisonCancelResult(cancelled=cancelled, metadata=outcome)

    # ── webhook parsing ──────────────────────────────────────────────────

    @staticmethod
    def parse_webhook_event(payload: dict[str, Any]) -> ParsedEmailBisonEvent:
        """Coverage doc §5 envelope:
            {"event": {"type": "EMAIL_SENT", "workspace_id": 1, ...},
             "data": {"scheduled_email": {...}, "campaign_event": {...},
                      "lead": {...}, "campaign": {...}, "sender_email": {...},
                      "reply": {...}}}

        Pull the join keys, normalize event type, and parse hqx:* tags off
        data.campaign.tags if present.
        """
        event_block = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event_block, dict):
            event_block = {}
        data_block = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data_block, dict):
            data_block = {}

        raw_event_name_value = (
            event_block.get("type")
            or payload.get("event_type")
            or payload.get("type")
            or ""
        )
        raw_event_name = (
            raw_event_name_value if isinstance(raw_event_name_value, str) else ""
        )
        event_type = _normalize_event_type(raw_event_name)

        workspace_id_raw = event_block.get("workspace_id")
        if workspace_id_raw is None:
            workspace_id_raw = payload.get("workspace_id")
        eb_workspace_id = (
            str(workspace_id_raw) if workspace_id_raw is not None else None
        )

        scheduled_email = data_block.get("scheduled_email")
        if not isinstance(scheduled_email, dict):
            scheduled_email = {}
        campaign = data_block.get("campaign")
        if not isinstance(campaign, dict):
            campaign = {}
        lead = data_block.get("lead")
        if not isinstance(lead, dict):
            lead = {}
        sender_email = data_block.get("sender_email")
        if not isinstance(sender_email, dict):
            sender_email = {}
        reply = data_block.get("reply")
        if not isinstance(reply, dict):
            reply = {}
        campaign_event = data_block.get("campaign_event")
        if not isinstance(campaign_event, dict):
            campaign_event = {}

        eb_scheduled_email_id = _coerce_int(scheduled_email.get("id"))
        eb_campaign_id = _coerce_int(campaign.get("id"))
        eb_lead_id = _coerce_int(lead.get("id")) or _coerce_int(
            scheduled_email.get("lead_id")
        )
        eb_sender_email_id = _coerce_int(sender_email.get("id"))
        eb_reply_id = _coerce_int(reply.get("id"))

        occurred_at_raw = (
            campaign_event.get("created_at")
            or scheduled_email.get("sent_at")
            or reply.get("date_received")
            or reply.get("created_at")
            or event_block.get("created_at")
            or payload.get("created_at")
            or payload.get("date_created")
        )

        tags_raw = campaign.get("tags") if isinstance(campaign, dict) else None
        six_tuple_tags = _parse_six_tuple_from_tags(tags_raw)

        return ParsedEmailBisonEvent(
            event_type=event_type,
            raw_event_name=raw_event_name,
            eb_workspace_id=eb_workspace_id,
            eb_campaign_id=eb_campaign_id,
            eb_scheduled_email_id=eb_scheduled_email_id,
            eb_lead_id=eb_lead_id,
            eb_sender_email_id=eb_sender_email_id,
            eb_reply_id=eb_reply_id,
            occurred_at_raw=occurred_at_raw,
            six_tuple_tags=six_tuple_tags,
            metadata={
                "raw_message_id": scheduled_email.get("raw_message_id"),
                "sequence_step_id": _coerce_int(
                    scheduled_email.get("sequence_step_id")
                ),
                "subject_snapshot": scheduled_email.get("email_subject"),
                "body_snapshot": scheduled_email.get("email_body"),
                "sender_email_snapshot": sender_email.get("email"),
                "lead_email": lead.get("email"),
                "campaign_event_id": _coerce_int(campaign_event.get("id")),
                "campaign_event_type": campaign_event.get("type"),
            },
        )


__all__ = [
    "EmailBisonActivationResult",
    "EmailBisonSendResult",
    "EmailBisonCancelResult",
    "ParsedEmailBisonEvent",
    "EmailBisonAdapter",
]
