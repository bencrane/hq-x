"""Trust Hub state-machine orchestration.

Ports OEX `services/trust_hub.py`. Per-brand instead of per-org/company
(single-operator world). Twilio creds come from the encrypted brands row
via `app.services.brands.get_twilio_creds`. Policy SIDs default to
well-known Twilio values; brands can override via the optional
`policy_sids` parameter.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio import trust_hub as twilio_trust_hub
from app.providers.twilio._http import TwilioProviderError
from app.providers.twilio.client import create_address

logger = logging.getLogger(__name__)


WELL_KNOWN_POLICY_SIDS = {
    "secondary_customer_profile": "RNdfbf3fae0e1107f8aded0e7cead80bf5",
    "shaken_stir": "RN7a97559effdf62d00f4298208492a5ea",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_base_url() -> str:
    return settings.HQX_API_BASE_URL.rstrip("/")


def _resolve_policy_sid(
    policy_sids: dict[str, str] | None,
    registration_type: str,
) -> str:
    """Resolve the Twilio Policy SID for a registration type."""
    config_key = registration_type
    if registration_type == "customer_profile":
        config_key = "secondary_customer_profile"

    if policy_sids:
        sid = policy_sids.get(config_key)
        if sid:
            return sid

    default = WELL_KNOWN_POLICY_SIDS.get(config_key)
    if default:
        return default

    raise ValueError(
        f"Policy SID for '{registration_type}' is not configured and has no well-known default. "
        f"Pass it via policy_sids[{config_key!r}]."
    )


# ---------------------------------------------------------------------------
# Internal: trust_hub_registrations row helpers
# ---------------------------------------------------------------------------


async def _insert_registration_row(
    *,
    registration_id: UUID,
    brand_id: UUID,
    registration_type: str,
    policy_sid: str,
    notification_email: str,
    customer_profile_sid: str | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO trust_hub_registrations (
                    id, brand_id, registration_type, status, policy_sid,
                    notification_email, customer_profile_sid
                )
                VALUES (%s, %s, %s, 'draft', %s, %s, %s)
                """,
                (
                    str(registration_id), str(brand_id), registration_type,
                    policy_sid, notification_email, customer_profile_sid,
                ),
            )
        await conn.commit()


async def _update_registration(
    registration_id: UUID,
    brand_id: UUID,
    fields: dict[str, Any],
) -> None:
    """Update a registration row. Caller passes a dict of column→value."""
    if not fields:
        return
    set_parts = []
    values: list[Any] = []
    for key, value in fields.items():
        set_parts.append(f"{key} = %s")
        if isinstance(value, dict):
            values.append(json.dumps(value))
        else:
            values.append(value)
    set_parts.append("updated_at = NOW()")
    values.extend([str(registration_id), str(brand_id)])
    sql = f"""
        UPDATE trust_hub_registrations
        SET {", ".join(set_parts)}
        WHERE id = %s AND brand_id = %s
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, values)
        await conn.commit()


async def _fail_registration(
    registration_id: UUID,
    brand_id: UUID,
    step: str,
    exc: Exception,
) -> None:
    await _update_registration(
        registration_id,
        brand_id,
        {"status": "failed", "error_details": {"step": step, "error": str(exc)}},
    )


async def get_registration(
    brand_id: UUID, registration_id: UUID
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, registration_type, status, bundle_sid,
                       policy_sid, end_user_business_sid, end_user_rep1_sid,
                       end_user_rep2_sid, address_sid, supporting_document_sid,
                       customer_profile_sid, evaluation_sid, evaluation_status,
                       evaluation_results, error_details, notification_email,
                       submitted_at, approved_at, rejected_at,
                       created_at, updated_at
                FROM trust_hub_registrations
                WHERE id = %s AND brand_id = %s
                """,
                (str(registration_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    cols = [
        "id", "brand_id", "registration_type", "status", "bundle_sid",
        "policy_sid", "end_user_business_sid", "end_user_rep1_sid",
        "end_user_rep2_sid", "address_sid", "supporting_document_sid",
        "customer_profile_sid", "evaluation_sid", "evaluation_status",
        "evaluation_results", "error_details", "notification_email",
        "submitted_at", "approved_at", "rejected_at",
        "created_at", "updated_at",
    ]
    return dict(zip(cols, row, strict=True))


async def get_registration_by_bundle_sid(
    bundle_sid: str,
) -> dict[str, Any] | None:
    """Look up a registration by Twilio bundle SID. Used by webhook handlers."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, registration_type, status
                FROM trust_hub_registrations
                WHERE bundle_sid = %s
                LIMIT 1
                """,
                (bundle_sid,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "brand_id": row[1],
        "registration_type": row[2], "status": row[3],
    }


# ---------------------------------------------------------------------------
# Customer Profile (Secondary Business Profile)
# ---------------------------------------------------------------------------


async def register_customer_profile(
    *,
    brand_id: UUID,
    account_sid: str,
    auth_token: str,
    primary_customer_profile_sid: str,
    notification_email: str,
    business_info: dict[str, Any],
    representative: dict[str, Any],
    representative_2: dict[str, Any] | None,
    address: dict[str, Any],
    policy_sids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Orchestrate full ISV Secondary Customer Profile creation workflow."""

    if not primary_customer_profile_sid:
        raise ValueError("primary_customer_profile_sid is required")

    policy_sid = _resolve_policy_sid(policy_sids, "customer_profile")

    registration_id = uuid4()
    await _insert_registration_row(
        registration_id=registration_id,
        brand_id=brand_id,
        registration_type="customer_profile",
        policy_sid=policy_sid,
        notification_email=notification_email,
    )

    status_callback_url = f"{_api_base_url()}/api/webhooks/twilio-trust-hub/{brand_id}"

    bundle_sid: str | None = None
    end_user_business_sid: str | None = None
    end_user_rep1_sid: str | None = None
    end_user_rep2_sid: str | None = None
    address_sid: str | None = None
    supporting_document_sid: str | None = None

    # 1. EndUser (business info)
    try:
        biz_result = twilio_trust_hub.create_end_user(
            account_sid, auth_token,
            friendly_name=f"{business_info['business_name']} - Business Info",
            type="customer_profile_business_information",
            attributes=business_info,
        )
        end_user_business_sid = biz_result["sid"]
        await _update_registration(
            registration_id, brand_id,
            {"end_user_business_sid": end_user_business_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_end_user_business", exc)
        raise

    # 2. EndUser (auth rep 1)
    try:
        rep1_result = twilio_trust_hub.create_end_user(
            account_sid, auth_token,
            friendly_name=f"{representative['first_name']} {representative['last_name']} - Auth Rep",
            type="authorized_representative_1",
            attributes=representative,
        )
        end_user_rep1_sid = rep1_result["sid"]
        await _update_registration(
            registration_id, brand_id,
            {"end_user_rep1_sid": end_user_rep1_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_end_user_rep1", exc)
        raise

    # 3. EndUser (auth rep 2) — optional
    if representative_2:
        try:
            rep2_result = twilio_trust_hub.create_end_user(
                account_sid, auth_token,
                friendly_name=f"{representative_2['first_name']} {representative_2['last_name']} - Auth Rep 2",
                type="authorized_representative_2",
                attributes=representative_2,
            )
            end_user_rep2_sid = rep2_result["sid"]
            await _update_registration(
                registration_id, brand_id,
                {"end_user_rep2_sid": end_user_rep2_sid},
            )
        except TwilioProviderError as exc:
            await _fail_registration(registration_id, brand_id, "create_end_user_rep2", exc)
            raise

    # 4. Address
    try:
        addr_result = create_address(
            account_sid, auth_token,
            customer_name=address["customer_name"],
            street=address["street"],
            city=address["city"],
            region=address["region"],
            postal_code=address["postal_code"],
            iso_country=address["iso_country"],
            street_secondary=address.get("street_secondary"),
            friendly_name=f"{business_info['business_name']} - Business Address",
        )
        address_sid = addr_result["sid"]
        await _update_registration(
            registration_id, brand_id,
            {"address_sid": address_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_address", exc)
        raise

    # 5. SupportingDocument
    try:
        doc_result = twilio_trust_hub.create_supporting_document(
            account_sid, auth_token,
            friendly_name=f"{business_info['business_name']} - Address Document",
            type="customer_profile_address",
            attributes={"address_sids": address_sid},
        )
        supporting_document_sid = doc_result["sid"]
        await _update_registration(
            registration_id, brand_id,
            {"supporting_document_sid": supporting_document_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_supporting_document", exc)
        raise

    # 6. CustomerProfile bundle
    try:
        profile_result = twilio_trust_hub.create_customer_profile(
            account_sid, auth_token,
            friendly_name=f"{business_info['business_name']} - Secondary Profile",
            email=notification_email,
            policy_sid=policy_sid,
            status_callback=status_callback_url,
        )
        bundle_sid = profile_result["sid"]
        await _update_registration(
            registration_id, brand_id,
            {"bundle_sid": bundle_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_customer_profile", exc)
        raise

    # 7. EntityAssignments
    try:
        twilio_trust_hub.create_entity_assignment(
            account_sid, auth_token,
            bundle_sid=bundle_sid, object_sid=end_user_business_sid,
        )
        twilio_trust_hub.create_entity_assignment(
            account_sid, auth_token,
            bundle_sid=bundle_sid, object_sid=end_user_rep1_sid,
        )
        if end_user_rep2_sid:
            twilio_trust_hub.create_entity_assignment(
                account_sid, auth_token,
                bundle_sid=bundle_sid, object_sid=end_user_rep2_sid,
            )
        twilio_trust_hub.create_entity_assignment(
            account_sid, auth_token,
            bundle_sid=bundle_sid, object_sid=supporting_document_sid,
        )
        twilio_trust_hub.create_entity_assignment(
            account_sid, auth_token,
            bundle_sid=bundle_sid, object_sid=primary_customer_profile_sid,
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_entity_assignments", exc)
        raise

    # 8. Evaluation
    try:
        eval_result = twilio_trust_hub.create_evaluation(
            account_sid, auth_token,
            bundle_sid=bundle_sid,
            policy_sid=policy_sid,
        )
        evaluation_sid = eval_result["sid"]
        evaluation_status = eval_result.get("status")
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_evaluation", exc)
        raise

    # 9. Submit if compliant
    submitted_at: datetime | None = None
    error_details: dict[str, Any] | None = None
    if evaluation_status == "compliant":
        try:
            twilio_trust_hub.update_customer_profile(
                account_sid, auth_token,
                customer_profile_sid=bundle_sid,
                status="pending-review",
            )
            new_status = "pending-review"
            submitted_at = _now()
        except TwilioProviderError as exc:
            await _fail_registration(registration_id, brand_id, "submit_customer_profile", exc)
            raise
    else:
        new_status = "draft"
        error_details = {"evaluation_results": eval_result.get("results")}

    final: dict[str, Any] = {
        "status": new_status,
        "evaluation_sid": evaluation_sid,
        "evaluation_status": evaluation_status,
        "evaluation_results": eval_result.get("results"),
    }
    if submitted_at is not None:
        final["submitted_at"] = submitted_at
    if error_details is not None:
        final["error_details"] = error_details
    await _update_registration(registration_id, brand_id, final)

    logger.info(
        "trust_hub_customer_profile_registered",
        extra={"brand_id": str(brand_id), "bundle_sid": bundle_sid, "status": new_status},
    )

    return {
        "registration_id": str(registration_id),
        "registration_type": "customer_profile",
        "status": new_status,
        "bundle_sid": bundle_sid,
        "evaluation_status": evaluation_status,
    }


# ---------------------------------------------------------------------------
# Trust Product (SHAKEN/STIR, A2P 10DLC, CNAM)
# ---------------------------------------------------------------------------


async def create_trust_product_registration(
    *,
    brand_id: UUID,
    account_sid: str,
    auth_token: str,
    notification_email: str,
    registration_type: str,
    customer_profile_sid: str,
    policy_sids: dict[str, str] | None = None,
) -> dict[str, Any]:
    if registration_type == "customer_profile":
        raise ValueError("Use register_customer_profile for customer_profile registrations")
    if not customer_profile_sid:
        raise ValueError("customer_profile_sid is required for trust product registrations")

    policy_sid = _resolve_policy_sid(policy_sids, registration_type)

    registration_id = uuid4()
    await _insert_registration_row(
        registration_id=registration_id,
        brand_id=brand_id,
        registration_type=registration_type,
        policy_sid=policy_sid,
        notification_email=notification_email,
        customer_profile_sid=customer_profile_sid,
    )

    status_callback_url = f"{_api_base_url()}/api/webhooks/twilio-trust-hub/{brand_id}"
    friendly_name_map = {
        "shaken_stir": "SHAKEN/STIR",
        "a2p_campaign": "A2P 10DLC Campaign",
        "cnam": "CNAM Branded Calling",
    }

    bundle_sid: str | None = None

    try:
        product_result = twilio_trust_hub.create_trust_product(
            account_sid, auth_token,
            friendly_name=f"Brand - {friendly_name_map.get(registration_type, registration_type)}",
            email=notification_email,
            policy_sid=policy_sid,
            status_callback=status_callback_url,
        )
        bundle_sid = product_result["sid"]
        await _update_registration(
            registration_id, brand_id, {"bundle_sid": bundle_sid},
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_trust_product", exc)
        raise

    try:
        twilio_trust_hub.create_entity_assignment(
            account_sid, auth_token,
            bundle_sid=bundle_sid,
            object_sid=customer_profile_sid,
            bundle_type="TrustProducts",
        )
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_entity_assignment", exc)
        raise

    try:
        eval_result = twilio_trust_hub.create_evaluation(
            account_sid, auth_token,
            bundle_sid=bundle_sid,
            policy_sid=policy_sid,
            bundle_type="TrustProducts",
        )
        evaluation_sid = eval_result["sid"]
        evaluation_status = eval_result.get("status")
    except TwilioProviderError as exc:
        await _fail_registration(registration_id, brand_id, "create_evaluation", exc)
        raise

    submitted_at: datetime | None = None
    error_details: dict[str, Any] | None = None
    if evaluation_status == "compliant":
        try:
            twilio_trust_hub.update_trust_product(
                account_sid, auth_token,
                trust_product_sid=bundle_sid,
                status="pending-review",
            )
            new_status = "pending-review"
            submitted_at = _now()
        except TwilioProviderError as exc:
            await _fail_registration(registration_id, brand_id, "submit_trust_product", exc)
            raise
    else:
        new_status = "draft"
        error_details = {"evaluation_results": eval_result.get("results")}

    final: dict[str, Any] = {
        "status": new_status,
        "evaluation_sid": evaluation_sid,
        "evaluation_status": evaluation_status,
        "evaluation_results": eval_result.get("results"),
    }
    if submitted_at is not None:
        final["submitted_at"] = submitted_at
    if error_details is not None:
        final["error_details"] = error_details
    await _update_registration(registration_id, brand_id, final)

    logger.info(
        "trust_hub_trust_product_registered",
        extra={
            "brand_id": str(brand_id),
            "registration_type": registration_type,
            "bundle_sid": bundle_sid,
            "status": new_status,
        },
    )

    return {
        "registration_id": str(registration_id),
        "registration_type": registration_type,
        "status": new_status,
        "bundle_sid": bundle_sid,
        "evaluation_status": evaluation_status,
    }


# ---------------------------------------------------------------------------
# Phone-number assignment + status refresh
# ---------------------------------------------------------------------------


def assign_phone_number_to_bundle(
    *,
    account_sid: str,
    auth_token: str,
    phone_number_sid: str,
    bundle_sid: str,
    bundle_type: str = "CustomerProfiles",
) -> dict[str, Any]:
    return twilio_trust_hub.create_channel_endpoint_assignment(
        account_sid, auth_token,
        bundle_sid=bundle_sid,
        channel_endpoint_type="phone-number",
        channel_endpoint_sid=phone_number_sid,
        bundle_type=bundle_type,
    )


async def refresh_registration_status(
    *,
    brand_id: UUID,
    registration_id: UUID,
    account_sid: str,
    auth_token: str,
) -> dict[str, Any]:
    """Poll Twilio for the current bundle status; update local row on change."""
    reg = await get_registration(brand_id, registration_id)
    if not reg:
        raise ValueError("Registration not found")

    bundle_sid = reg.get("bundle_sid")
    if not bundle_sid:
        raise ValueError("Registration has no bundle_sid — not yet created on Twilio")

    registration_type = reg["registration_type"]
    if registration_type == "customer_profile":
        twilio_data = twilio_trust_hub.get_customer_profile(
            account_sid, auth_token,
            customer_profile_sid=bundle_sid,
        )
    else:
        twilio_data = twilio_trust_hub.get_trust_product(
            account_sid, auth_token,
            trust_product_sid=bundle_sid,
        )

    twilio_status = twilio_data.get("status")
    local_status = reg.get("status")

    if twilio_status and twilio_status != local_status:
        update: dict[str, Any] = {"status": twilio_status}
        if twilio_status == "twilio-approved":
            update["approved_at"] = _now()
        elif twilio_status == "twilio-rejected":
            update["rejected_at"] = _now()
            if twilio_data.get("errors"):
                update["error_details"] = {"twilio_errors": twilio_data["errors"]}
        await _update_registration(registration_id, brand_id, update)
        reg.update(update)

    return reg


async def apply_callback_status_update(
    *,
    bundle_sid: str,
    new_status: str,
    twilio_payload: dict[str, Any],
) -> bool:
    """Apply a Twilio Trust Hub callback's status update to the registration row.

    Used by the trust_hub webhook receiver. Returns True if a row was found
    and updated, False otherwise.
    """
    reg = await get_registration_by_bundle_sid(bundle_sid)
    if reg is None:
        return False
    update: dict[str, Any] = {"status": new_status}
    if new_status == "twilio-approved":
        update["approved_at"] = _now()
    elif new_status == "twilio-rejected":
        update["rejected_at"] = _now()
        errors = twilio_payload.get("FailureReason") or twilio_payload.get("Errors")
        if errors:
            update["error_details"] = {"twilio_errors": errors}
    await _update_registration(reg["id"], reg["brand_id"], update)
    return True
