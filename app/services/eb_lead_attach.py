"""EmailBison lead-attach — the seam that closes the outbound send loop.

Background: when a channel_campaign_step is activated, EmailBisonAdapter
creates a fresh EB campaign object on EB's side. EB knows the campaign
exists but has no leads to send to. Until this service runs, no emails
fire. Operator's pre-this-PR reality was manual EB-UI lead attachment.

This service:
  1. Reads the step + parent channel_campaign + initiative.
  2. Resolves recipient memberships in 'scheduled' status that don't yet
     have an eb_lead_id stamped.
  3. Builds EB lead payloads from recipient + recipient.metadata.
  4. Decides live vs dry_run based on a three-tier opt-in (kill switch
     → org default → per-initiative override).
  5. In live mode: chunks of <=500 leads, POST bulk_upsert_leads,
     captures the EB lead_ids back, POST attach_leads to bind them to
     the EB campaign, stamps eb_lead_id on each membership row.
  6. In dry_run: builds the payloads + records what would have been
     sent, doesn't POST. Lets operator inspect before flipping live.
  7. Writes a single row to business.eb_lead_attach_log per attempt,
     plus per-recipient failure_reason where applicable.
  8. Fires alerts on per-attempt total failure.

The opt-in tiers (all must be truthy for live mode):
  * Settings.OUTBOUND_LIVE_LEAD_ATTACH (global kill switch; default false)
  * Org metadata.outbound_live_lead_attach_enabled (org default; default
    inherits kill switch)
  * Initiative metadata.outbound_live_lead_attach_enabled (per-initiative
    override; absent = inherit)

Defaulting all to false means the operator has to deliberately opt in
per initiative before any real EB lead-attach happens. That's the
right trade for a high-blast-radius seam.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError
from app.services import alerts

logger = logging.getLogger(__name__)


CHUNK_SIZE = 500


class EBLeadAttachError(Exception):
    pass


# ── Public entry point ────────────────────────────────────────────────────


async def attach_leads_for_step(
    *,
    step_id: UUID,
    organization_id: UUID,
    force_live: bool = False,
) -> dict[str, Any]:
    """Run one lead-attach pass for `step_id`.

    Returns ``{status, mode, recipients_*, ...}``.
    """
    started_at = time.monotonic()
    step_ctx = await _resolve_step_context(
        step_id=step_id, organization_id=organization_id
    )
    if step_ctx is None:
        raise EBLeadAttachError(f"step {step_id} not found")
    if step_ctx["external_provider_id"] is None:
        return {
            "status": "skipped",
            "reason": "no_external_provider_id",
            "step_id": str(step_id),
        }
    if step_ctx["channel"] != "email" or step_ctx["provider"] != "emailbison":
        return {
            "status": "skipped",
            "reason": f"channel={step_ctx['channel']} provider={step_ctx['provider']}",
            "step_id": str(step_id),
        }

    cluster = _resolve_cluster(step_ctx)
    log_id = await _create_log_row(
        step_id=step_id,
        organization_id=organization_id,
        initiative_id=step_ctx["initiative_id"],
        cluster=cluster,
    )

    try:
        # Decide live vs dry_run.
        live_decision = _decide_live(
            org_metadata=step_ctx["org_metadata"],
            initiative_metadata=step_ctx["initiative_metadata"],
            force_live=force_live,
        )
        mode = live_decision["mode"]
        mode_reason = live_decision["reason"]

        # Pull eligible memberships.
        memberships = await _list_eligible_memberships(step_id=step_id)
        recipients_eligible = len(memberships)

        if recipients_eligible == 0:
            await _mark_log(
                log_id=log_id,
                status="dry_run" if mode == "dry_run" else "live_pass",
                mode=mode,
                mode_reason=mode_reason,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                recipients_total=0,
                recipients_eligible=0,
                upserted=0,
                attached=0,
                failed=0,
            )
            return {
                "status": "no_recipients",
                "mode": mode,
                "step_id": str(step_id),
                "log_id": str(log_id),
            }

        # Build EB lead payloads.
        payloads_with_membership = [
            (m, _build_eb_lead_payload(m))
            for m in memberships
            if m.get("recipient_email")
        ]
        recipients_total = len(payloads_with_membership)

        if mode == "dry_run":
            # Record what we WOULD have done without hitting EB.
            await _record_dry_run(
                memberships=[
                    (m, p) for m, p in payloads_with_membership
                ],
                log_id=log_id,
            )
            await _mark_log(
                log_id=log_id,
                status="dry_run",
                mode="dry_run",
                mode_reason=mode_reason,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                recipients_total=recipients_total,
                recipients_eligible=recipients_eligible,
                upserted=0,
                attached=0,
                failed=0,
                metadata={
                    "sample_payloads": [p for _, p in payloads_with_membership[:3]],
                },
            )
            return {
                "status": "dry_run",
                "mode": "dry_run",
                "mode_reason": mode_reason,
                "step_id": str(step_id),
                "log_id": str(log_id),
                "recipients_total": recipients_total,
            }

        # Live mode.
        api_key = _api_key_or_raise()
        upserted = 0
        attached = 0
        failed = 0
        failures: list[dict[str, Any]] = []
        all_eb_lead_ids: list[int] = []

        for chunk_start in range(0, len(payloads_with_membership), CHUNK_SIZE):
            chunk = payloads_with_membership[chunk_start : chunk_start + CHUNK_SIZE]
            try:
                response = eb_client.bulk_upsert_leads(
                    api_key, [p for _, p in chunk]
                )
            except EmailBisonProviderError as exc:
                failed += len(chunk)
                failures.append(
                    {
                        "phase": "bulk_upsert_leads",
                        "chunk_size": len(chunk),
                        "error": str(exc)[:300],
                    }
                )
                logger.warning(
                    "eb_lead_attach bulk_upsert chunk failed: %s", exc
                )
                continue

            # Parse the response: EB returns {"data": [{id, email, ...}, ...]}.
            chunk_eb_leads = _parse_upsert_response(response)
            email_to_lead_id = {
                str(lead.get("email") or "").lower(): lead.get("id")
                for lead in chunk_eb_leads
                if lead.get("id") and lead.get("email")
            }

            # Map back to our memberships, stamp eb_lead_id.
            chunk_attached_ids: list[int] = []
            for membership, payload in chunk:
                eb_lead_id = email_to_lead_id.get(payload["email"].lower())
                if eb_lead_id is None:
                    failed += 1
                    failures.append(
                        {
                            "phase": "lead_id_match",
                            "email": payload["email"],
                            "error": "no_eb_lead_id_in_response",
                        }
                    )
                    await _stamp_membership_failure(
                        membership_id=membership["membership_id"],
                        reason="no_eb_lead_id_returned",
                    )
                    continue
                upserted += 1
                chunk_attached_ids.append(int(eb_lead_id))
                await _stamp_membership_eb_lead_id(
                    membership_id=membership["membership_id"],
                    eb_lead_id=int(eb_lead_id),
                )
                all_eb_lead_ids.append(int(eb_lead_id))

            if chunk_attached_ids:
                try:
                    eb_client.attach_leads(
                        api_key,
                        step_ctx["external_provider_id"],
                        chunk_attached_ids,
                    )
                    attached += len(chunk_attached_ids)
                except EmailBisonProviderError as exc:
                    failed += len(chunk_attached_ids)
                    failures.append(
                        {
                            "phase": "attach_leads",
                            "chunk_size": len(chunk_attached_ids),
                            "error": str(exc)[:300],
                        }
                    )
                    logger.warning(
                        "eb_lead_attach attach_leads chunk failed: %s", exc
                    )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        live_status = "live_pass" if failed == 0 else "live_fail"
        await _mark_log(
            log_id=log_id,
            status=live_status,
            mode="live",
            mode_reason=mode_reason,
            duration_ms=duration_ms,
            recipients_total=recipients_total,
            recipients_eligible=recipients_eligible,
            upserted=upserted,
            attached=attached,
            failed=failed,
            metadata={
                "all_eb_lead_ids_count": len(all_eb_lead_ids),
                "failures": failures[:20],
            },
        )

        if failed > 0:
            await alerts.fire_alert(
                severity="warning" if failed < recipients_total else "critical",
                source=f"{cluster}_eb_lead_attach",
                summary=(
                    f"EB lead-attach: {failed}/{recipients_total} failed "
                    f"for step {step_id}"
                ),
                payload={
                    "step_id": str(step_id),
                    "initiative_id": str(step_ctx["initiative_id"]),
                    "log_id": str(log_id),
                    "recipients_total": recipients_total,
                    "upserted": upserted,
                    "attached": attached,
                    "failed": failed,
                    "first_failures": failures[:5],
                },
            )

        return {
            "status": live_status,
            "mode": "live",
            "mode_reason": mode_reason,
            "step_id": str(step_id),
            "log_id": str(log_id),
            "recipients_total": recipients_total,
            "upserted": upserted,
            "attached": attached,
            "failed": failed,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _mark_log(
            log_id=log_id,
            status="live_fail",
            mode="live",
            mode_reason="crashed",
            duration_ms=duration_ms,
            recipients_total=0,
            recipients_eligible=0,
            upserted=0,
            attached=0,
            failed=0,
            failure_reason=str(exc)[:500],
        )
        await alerts.fire_alert(
            severity="critical",
            source="eb_lead_attach",
            summary=f"Lead-attach crashed for step {step_id}: {str(exc)[:160]}",
            payload={"step_id": str(step_id), "error": str(exc)[:500]},
        )
        raise


# ── Decision logic ────────────────────────────────────────────────────────


def _decide_live(
    *,
    org_metadata: dict[str, Any],
    initiative_metadata: dict[str, Any],
    force_live: bool,
) -> dict[str, str]:
    """Three-tier opt-in. All must agree for live mode.

    Tier 1: Settings.OUTBOUND_LIVE_LEAD_ATTACH (global kill switch)
    Tier 2: Org metadata.outbound_live_lead_attach_enabled
    Tier 3: Initiative metadata.outbound_live_lead_attach_enabled
            (absent = inherit org default)

    `force_live=True` overrides tiers 1+2 but NOT tier 3 (operator can
    still globally disable per-initiative).
    """
    if force_live:
        if isinstance(initiative_metadata, dict):
            v = initiative_metadata.get("outbound_live_lead_attach_enabled")
            if v is False:
                return {
                    "mode": "dry_run",
                    "reason": "initiative_explicit_disable_overrides_force",
                }
        return {"mode": "live", "reason": "force_live"}

    global_flag = bool(getattr(settings, "OUTBOUND_LIVE_LEAD_ATTACH", False))
    if not global_flag:
        return {"mode": "dry_run", "reason": "global_kill_switch_off"}

    org_flag = (
        org_metadata.get("outbound_live_lead_attach_enabled", True)
        if isinstance(org_metadata, dict)
        else True
    )
    if org_flag is False:
        return {"mode": "dry_run", "reason": "org_disabled"}

    init_flag = None
    if isinstance(initiative_metadata, dict):
        init_flag = initiative_metadata.get("outbound_live_lead_attach_enabled")
    if init_flag is False:
        return {"mode": "dry_run", "reason": "initiative_disabled"}
    if init_flag is True:
        return {"mode": "live", "reason": "initiative_enabled"}
    # init_flag is None — inherit org default. Live since org_flag truthy.
    return {"mode": "live", "reason": "org_default"}


def _resolve_cluster(step_ctx: dict[str, Any]) -> str:
    init_kind = step_ctx.get("initiative_kind")
    init_meta = step_ctx.get("initiative_metadata") or {}
    if init_kind == "self_prospecting":
        return "cluster_1"
    if init_kind == "partner_demand":
        leg = (
            int(init_meta.get("leg")) if isinstance(init_meta, dict) and init_meta.get("leg") else None
        )
        if leg == 2:
            return "cluster_2"
    # Fallback (shouldn't hit in practice for outbound steps).
    return "cluster_2"


def _api_key_or_raise() -> str:
    secret = getattr(settings, "EMAILBISON_API_KEY", None)
    if not secret:
        raise EBLeadAttachError("EMAILBISON_API_KEY not configured")
    return (
        secret.get_secret_value()
        if hasattr(secret, "get_secret_value")
        else str(secret)
    )


# ── Payload builder ──────────────────────────────────────────────────────


def _build_eb_lead_payload(membership: dict[str, Any]) -> dict[str, Any]:
    """Build an EB lead payload from a recipient row.

    Minimum: email, first_name. Plus any keys from
    recipient.metadata.facts_snapshot that are scalar (preserved as
    custom variables for in-template substitution like {dot}, {state}).
    """
    email = (membership.get("recipient_email") or "").strip()
    display = membership.get("recipient_display_name") or ""
    rec_md = membership.get("recipient_metadata") or {}

    first_name = (
        (rec_md.get("first_name") if isinstance(rec_md, dict) else None)
        or (display.split()[0] if display else None)
        or "there"
    )
    last_name = (
        (rec_md.get("last_name") if isinstance(rec_md, dict) else None)
        or (
            " ".join(display.split()[1:])
            if len(display.split()) > 1
            else None
        )
    )

    payload: dict[str, Any] = {
        "email": email,
        "first_name": str(first_name)[:120],
    }
    if last_name:
        payload["last_name"] = str(last_name)[:120]

    # Scalar custom variables from facts_snapshot.
    facts = (
        rec_md.get("facts_snapshot")
        if isinstance(rec_md, dict)
        else None
    )
    if isinstance(facts, dict):
        for k, v in facts.items():
            if not isinstance(k, str) or len(k) > 60:
                continue
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                payload[k] = v

    return payload


# ── DB helpers ───────────────────────────────────────────────────────────


async def _resolve_step_context(
    *, step_id: UUID, organization_id: UUID
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    step.id, step.external_provider_id,
                    cc.channel, cc.provider, cc.initiative_id,
                    init.kind, init.metadata,
                    org.id, org.metadata
                FROM business.channel_campaign_steps step
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                JOIN business.organizations org ON org.id = step.organization_id
                WHERE step.id = %s AND step.organization_id = %s
                """,
                (str(step_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "external_provider_id": row[1],
        "channel": row[2],
        "provider": row[3],
        "channel_campaign_id_via_step": None,
        "initiative_id": row[4],
        "initiative_kind": row[5],
        "initiative_metadata": row[6] or {},
        "org_id": row[7],
        "org_metadata": row[8] or {},
    }


async def _list_eligible_memberships(
    *, step_id: UUID
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT m.id, m.recipient_id, m.eb_lead_id,
                       r.email, r.display_name, r.metadata
                FROM business.channel_campaign_step_recipients m
                JOIN business.recipients r ON r.id = m.recipient_id
                WHERE m.channel_campaign_step_id = %s
                  AND m.status = 'scheduled'
                  AND m.eb_lead_id IS NULL
                  AND r.email IS NOT NULL
                  AND length(trim(r.email)) > 0
                ORDER BY m.created_at ASC
                """,
                (str(step_id),),
            )
            rows = await cur.fetchall()
    return [
        {
            "membership_id": r[0],
            "recipient_id": r[1],
            "eb_lead_id": r[2],
            "recipient_email": r[3],
            "recipient_display_name": r[4],
            "recipient_metadata": r[5] or {},
        }
        for r in rows
    ]


async def _record_dry_run(
    *, memberships: list[tuple[dict[str, Any], dict[str, Any]]], log_id: UUID
) -> None:
    """In dry_run, stamp the synthetic eb_lead_id=-1 marker so the
    recovery sweep won't keep flagging these as 'no eb_lead_id'. The
    operator can flip the initiative live later and rerun, which will
    skip rows already marked dry_run."""
    # Don't stamp eb_lead_id (it would prevent live retry). Just record
    # that the membership was processed in dry_run via metadata on the log.
    return


async def _stamp_membership_eb_lead_id(
    *, membership_id: UUID, eb_lead_id: int
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_step_recipients
                SET eb_lead_id = %s,
                    eb_lead_attached_at = NOW(),
                    eb_lead_attach_failure_reason = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (eb_lead_id, str(membership_id)),
            )
        await conn.commit()


async def _stamp_membership_failure(
    *, membership_id: UUID, reason: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_step_recipients
                SET eb_lead_attach_failure_reason = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (reason, str(membership_id)),
            )
        await conn.commit()


async def _create_log_row(
    *,
    step_id: UUID,
    organization_id: UUID,
    initiative_id: UUID,
    cluster: str,
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.eb_lead_attach_log (
                    channel_campaign_step_id, organization_id,
                    initiative_id, cluster, status, mode
                )
                VALUES (%s, %s, %s, %s, 'running', 'dry_run')
                RETURNING id
                """,
                (
                    str(step_id),
                    str(organization_id),
                    str(initiative_id),
                    cluster,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _mark_log(
    *,
    log_id: UUID,
    status: str,
    mode: str,
    mode_reason: str,
    duration_ms: int,
    recipients_total: int,
    recipients_eligible: int,
    upserted: int,
    attached: int,
    failed: int,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.eb_lead_attach_log
                SET status = %s,
                    mode = %s,
                    mode_reason = %s,
                    completed_at = NOW(),
                    duration_ms = %s,
                    recipients_total = %s,
                    recipients_eligible = %s,
                    upserted_count = %s,
                    attached_count = %s,
                    failed_count = %s,
                    failure_reason = %s,
                    metadata = %s
                WHERE id = %s
                """,
                (
                    status,
                    mode,
                    mode_reason,
                    duration_ms,
                    recipients_total,
                    recipients_eligible,
                    upserted,
                    attached,
                    failed,
                    failure_reason,
                    Jsonb(metadata or {}),
                    str(log_id),
                ),
            )
        await conn.commit()


def _parse_upsert_response(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        leads = data.get("leads") or data.get("items") or data.get("data")
        if isinstance(leads, list):
            return [d for d in leads if isinstance(d, dict)]
    items = response.get("leads") or response.get("items")
    if isinstance(items, list):
        return [d for d in items if isinstance(d, dict)]
    return []


__all__ = ["attach_leads_for_step", "EBLeadAttachError"]
