"""Voice provisioning pipeline (brand-axis).

Brand-axis port of OEX ``services/pipelines/voice.py``. The OEX version
threaded an ``org_id``/``company_id`` and persisted state via the
``provisioning_run_steps`` ledger and the ``company_provisioning_runs`` row.
hq-x has no companies, no provisioning ledger, and a single brand axis —
so this version returns a structured result dict instead of writing to a
ledger. Callers that want history can read the side-effect rows in
``trust_hub_registrations``, ``voice_phone_numbers``, ``ivr_phone_configs``.

The 8 steps:
  1. Trust Hub Customer Profile (only if business_info provided)
  2. TwiML application creation
  3. Phone number search + purchase
  4. SHAKEN/STIR registration (depends on 1)
  5. A2P 10DLC campaign registration (depends on 1, only if SMS enabled)
  6. Phone-number → Trust Hub assignment (depends on 1, 3)
  7. IVR template attachment (depends on 3, only if ivr_template_flow_id provided)

Each step records (status, result_data, error) inside the returned dict.
A failure in one step does not abort later independent steps; dependent
steps are marked "skipped_dependency" instead.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.db import get_db_connection
from app.providers.twilio import client as twilio_client
from app.providers.twilio._http import TwilioProviderError
from app.services import brands as brands_svc
from app.services import trust_hub as trust_hub_svc

logger = logging.getLogger(__name__)


STEP_TRUST_HUB_PROFILE = "trust_hub_profile"
STEP_TWIML_APP = "twiml_app"
STEP_PHONE_PURCHASE = "phone_purchase"
STEP_SHAKEN_STIR = "shaken_stir"
STEP_A2P_CAMPAIGN = "a2p_campaign"
STEP_PHONE_TRUST_ASSIGNMENT = "phone_trust_assignment"
STEP_IVR_TEMPLATE = "ivr_template"


def _api_base_url() -> str:
    base = settings.HQX_API_BASE_URL or ""
    return str(base).rstrip("/")


async def _attach_ivr_template(
    *,
    brand_id: UUID,
    template_flow_id: UUID,
    phone_number: str,
    phone_number_sid: str | None,
) -> dict[str, Any]:
    """Attach the IVR flow template to the purchased phone number.

    Idempotent: if a row exists for ``phone_number``, it is reactivated and
    its flow_id is updated. Otherwise insert.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM ivr_phone_configs
                WHERE phone_number = %s AND brand_id = %s
                """,
                (phone_number, str(brand_id)),
            )
            existing = await cur.fetchone()
            if existing is not None:
                await cur.execute(
                    """
                    UPDATE ivr_phone_configs
                    SET flow_id = %s, phone_number_sid = %s,
                        is_active = TRUE, deleted_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (str(template_flow_id), phone_number_sid, existing[0]),
                )
                row = await cur.fetchone()
            else:
                await cur.execute(
                    """
                    INSERT INTO ivr_phone_configs (
                        brand_id, phone_number, phone_number_sid, flow_id, is_active
                    ) VALUES (%s, %s, %s, %s, TRUE)
                    RETURNING id
                    """,
                    (
                        str(brand_id), phone_number, phone_number_sid,
                        str(template_flow_id),
                    ),
                )
                row = await cur.fetchone()
        await conn.commit()
    return {"ivr_phone_config_id": row[0], "flow_id": str(template_flow_id)}


async def _record_phone_number(
    *,
    brand_id: UUID,
    phone_number: str,
    twilio_phone_number_sid: str,
) -> UUID:
    """Insert a row in voice_phone_numbers for a freshly purchased number."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO voice_phone_numbers (
                    brand_id, phone_number, twilio_phone_number_sid,
                    provider, purpose, status
                )
                VALUES (%s, %s, %s, 'twilio', 'inbound', 'active')
                ON CONFLICT (phone_number, brand_id) WHERE deleted_at IS NULL
                DO UPDATE SET
                    twilio_phone_number_sid = EXCLUDED.twilio_phone_number_sid,
                    status = 'active', updated_at = NOW()
                RETURNING id
                """,
                (str(brand_id), phone_number, twilio_phone_number_sid),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def execute_voice_pipeline(
    *,
    brand_id: UUID,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run the voice provisioning pipeline for a brand.

    config keys:
      - business_info: dict (optional, gates Trust Hub steps)
      - authorized_representative: dict (required if business_info)
      - authorized_representative_2: dict (optional)
      - address: dict (required if business_info)
      - notification_email: str (required if business_info)
      - phone_numbers_to_purchase: int (default 0)
      - phone_number_search: dict {area_code, country_code} (default {})
      - ivr_template_flow_id: UUID (optional, only if numbers purchased)
      - sms_enabled: bool (default False)

    Returns a dict of {step_name -> {status, ...}}.
    """
    business_info = config.get("business_info")
    authorized_rep = config.get("authorized_representative")
    authorized_rep_2 = config.get("authorized_representative_2")
    address = config.get("address")
    notification_email = config.get("notification_email")
    ivr_template_flow_id = config.get("ivr_template_flow_id")
    phone_count = int(config.get("phone_numbers_to_purchase", 0))
    phone_search = config.get("phone_number_search") or {}
    sms_enabled = bool(config.get("sms_enabled", False))

    has_business_info = business_info is not None
    needs_phones = phone_count > 0

    creds = await brands_svc.get_twilio_creds(brand_id)
    if creds is None:
        raise ValueError("Brand has no Twilio credentials configured")

    brand = await brands_svc.get_brand(brand_id)
    if brand is None:
        raise ValueError("Brand not found")

    result: dict[str, Any] = {"brand_id": str(brand_id), "steps": {}}

    # ----- Step 1: Trust Hub Customer Profile -----
    customer_profile_sid: str | None = None
    if has_business_info:
        if not (authorized_rep and address and notification_email):
            result["steps"][STEP_TRUST_HUB_PROFILE] = {
                "status": "failed",
                "error": "missing required Trust Hub fields",
            }
        else:
            try:
                cp_result = await trust_hub_svc.register_customer_profile(
                    brand_id=brand_id,
                    account_sid=creds.account_sid,
                    auth_token=creds.auth_token,
                    primary_customer_profile_sid=brand.primary_customer_profile_sid or "",
                    notification_email=notification_email,
                    business_info=business_info,
                    representative=authorized_rep,
                    representative_2=authorized_rep_2,
                    address=address,
                )
                customer_profile_sid = cp_result.get("bundle_sid")
                result["steps"][STEP_TRUST_HUB_PROFILE] = {
                    "status": "completed",
                    "result_data": cp_result,
                }
            except (TwilioProviderError, ValueError) as exc:
                logger.warning("trust_hub_profile failed brand=%s err=%s", brand_id, exc)
                result["steps"][STEP_TRUST_HUB_PROFILE] = {
                    "status": "failed",
                    "error": str(exc),
                }
    else:
        result["steps"][STEP_TRUST_HUB_PROFILE] = {"status": "skipped"}

    th_ok = result["steps"][STEP_TRUST_HUB_PROFILE].get("status") == "completed"

    # ----- Step 2: TwiML application -----
    twiml_app_sid: str | None = None
    try:
        api_base = _api_base_url()
        voice_url = f"{api_base}/api/voice/ivr/{brand_id}/entry" if api_base else None
        twiml_resp = twilio_client.create_application(
            creds.account_sid, creds.auth_token,
            friendly_name=f"hqx-{brand.name}-{uuid4().hex[:8]}",
            voice_url=voice_url,
            voice_method="POST",
        )
        twiml_app_sid = twiml_resp.get("sid")
        result["steps"][STEP_TWIML_APP] = {
            "status": "completed",
            "result_data": {"twiml_app_sid": twiml_app_sid, "voice_url": voice_url},
        }
    except TwilioProviderError as exc:
        logger.warning("twiml_app failed brand=%s err=%s", brand_id, exc)
        result["steps"][STEP_TWIML_APP] = {"status": "failed", "error": str(exc)}

    # ----- Step 3: Phone purchase -----
    purchased: list[dict[str, str]] = []  # [{phone_number, sid}]
    if needs_phones:
        try:
            available = twilio_client.search_available_numbers(
                creds.account_sid, creds.auth_token,
                country_code=phone_search.get("country_code", "US"),
                area_code=phone_search.get("area_code"),
                limit=max(phone_count, 1),
            )
            candidates = available.get("available_phone_numbers", []) or []
            if len(candidates) < phone_count:
                raise ValueError(
                    f"insufficient candidates: requested {phone_count}, found {len(candidates)}"
                )
            for cand in candidates[:phone_count]:
                ph = cand["phone_number"]
                purchase_resp = twilio_client.purchase_phone_number(
                    creds.account_sid, creds.auth_token,
                    phone_number=ph,
                    voice_application_sid=twiml_app_sid,
                )
                sid = purchase_resp.get("sid")
                purchased.append({"phone_number": ph, "sid": sid})
                if sid:
                    await _record_phone_number(
                        brand_id=brand_id, phone_number=ph,
                        twilio_phone_number_sid=sid,
                    )
            result["steps"][STEP_PHONE_PURCHASE] = {
                "status": "completed",
                "result_data": {"purchased": purchased},
            }
        except (TwilioProviderError, ValueError, KeyError) as exc:
            logger.warning("phone_purchase failed brand=%s err=%s", brand_id, exc)
            result["steps"][STEP_PHONE_PURCHASE] = {
                "status": "failed",
                "error": str(exc),
            }
    else:
        result["steps"][STEP_PHONE_PURCHASE] = {"status": "skipped"}

    phone_ok = result["steps"][STEP_PHONE_PURCHASE].get("status") == "completed"

    # ----- Step 4: SHAKEN/STIR -----
    if has_business_info:
        if th_ok and customer_profile_sid and notification_email:
            try:
                ss = await trust_hub_svc.create_trust_product_registration(
                    brand_id=brand_id,
                    account_sid=creds.account_sid,
                    auth_token=creds.auth_token,
                    notification_email=notification_email,
                    registration_type="shaken_stir",
                    customer_profile_sid=customer_profile_sid,
                )
                result["steps"][STEP_SHAKEN_STIR] = {
                    "status": "completed",
                    "result_data": ss,
                }
            except (TwilioProviderError, ValueError) as exc:
                logger.warning("shaken_stir failed brand=%s err=%s", brand_id, exc)
                result["steps"][STEP_SHAKEN_STIR] = {
                    "status": "failed",
                    "error": str(exc),
                }
        else:
            result["steps"][STEP_SHAKEN_STIR] = {"status": "skipped_dependency"}
    else:
        result["steps"][STEP_SHAKEN_STIR] = {"status": "skipped"}

    # ----- Step 5: A2P 10DLC -----
    if sms_enabled and has_business_info:
        if th_ok and customer_profile_sid and notification_email:
            try:
                a2p = await trust_hub_svc.create_trust_product_registration(
                    brand_id=brand_id,
                    account_sid=creds.account_sid,
                    auth_token=creds.auth_token,
                    notification_email=notification_email,
                    registration_type="a2p_campaign",
                    customer_profile_sid=customer_profile_sid,
                )
                result["steps"][STEP_A2P_CAMPAIGN] = {
                    "status": "completed",
                    "result_data": a2p,
                }
            except (TwilioProviderError, ValueError) as exc:
                logger.warning("a2p_campaign failed brand=%s err=%s", brand_id, exc)
                result["steps"][STEP_A2P_CAMPAIGN] = {
                    "status": "failed",
                    "error": str(exc),
                }
        else:
            result["steps"][STEP_A2P_CAMPAIGN] = {"status": "skipped_dependency"}
    else:
        result["steps"][STEP_A2P_CAMPAIGN] = {"status": "skipped"}

    # ----- Step 6: Phone → Trust Hub assignment -----
    if needs_phones and has_business_info:
        if th_ok and phone_ok and customer_profile_sid and purchased:
            assigned: list[dict[str, str]] = []
            errors: list[str] = []
            for p in purchased:
                try:
                    a = trust_hub_svc.assign_phone_number_to_bundle(
                        account_sid=creds.account_sid,
                        auth_token=creds.auth_token,
                        phone_number_sid=p["sid"],
                        bundle_sid=customer_profile_sid,
                    )
                    assigned.append({"sid": p["sid"], "assignment": a.get("sid", "")})
                except TwilioProviderError as exc:
                    errors.append(f"{p['sid']}: {exc}")
            if errors:
                result["steps"][STEP_PHONE_TRUST_ASSIGNMENT] = {
                    "status": "failed",
                    "error": "; ".join(errors),
                    "result_data": {"assigned": assigned},
                }
            else:
                result["steps"][STEP_PHONE_TRUST_ASSIGNMENT] = {
                    "status": "completed",
                    "result_data": {"assigned": assigned},
                }
        else:
            result["steps"][STEP_PHONE_TRUST_ASSIGNMENT] = {
                "status": "skipped_dependency"
            }
    else:
        result["steps"][STEP_PHONE_TRUST_ASSIGNMENT] = {"status": "skipped"}

    # ----- Step 7: IVR template attachment -----
    if ivr_template_flow_id and needs_phones:
        if phone_ok and purchased:
            try:
                first = purchased[0]
                ivr = await _attach_ivr_template(
                    brand_id=brand_id,
                    template_flow_id=UUID(str(ivr_template_flow_id)),
                    phone_number=first["phone_number"],
                    phone_number_sid=first["sid"],
                )
                result["steps"][STEP_IVR_TEMPLATE] = {
                    "status": "completed",
                    "result_data": ivr,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("ivr_template failed brand=%s err=%s", brand_id, exc)
                result["steps"][STEP_IVR_TEMPLATE] = {
                    "status": "failed",
                    "error": str(exc),
                }
        else:
            result["steps"][STEP_IVR_TEMPLATE] = {"status": "skipped_dependency"}
    else:
        result["steps"][STEP_IVR_TEMPLATE] = {"status": "skipped"}

    # Aggregate status
    statuses = [s.get("status") for s in result["steps"].values()]
    if any(st == "failed" for st in statuses):
        result["status"] = "partial"
    elif all(st in ("completed", "skipped", "skipped_dependency") for st in statuses):
        result["status"] = "completed"
    else:
        result["status"] = "unknown"

    return result
