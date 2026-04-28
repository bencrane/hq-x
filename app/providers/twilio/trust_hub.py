from __future__ import annotations

from typing import Any

from app.providers.twilio._http import request_json


TWILIO_TRUSTHUB_BASE = "https://trusthub.twilio.com"


# ---------------------------------------------------------------------------
# CustomerProfile (Secondary Business Profile per company)
# ---------------------------------------------------------------------------


def create_customer_profile(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str,
    email: str,
    policy_sid: str,
    status_callback: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Create a Secondary Customer Profile bundle."""
    body: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Email": email,
        "PolicySid": policy_sid,
    }
    if status_callback is not None:
        body["StatusCallback"] = status_callback

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/CustomerProfiles",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


def get_customer_profile(
    account_sid: str,
    auth_token: str,
    *,
    customer_profile_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch a Customer Profile by SID."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/CustomerProfiles/{customer_profile_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_customer_profile(
    account_sid: str,
    auth_token: str,
    *,
    customer_profile_sid: str,
    status: str | None = None,
    friendly_name: str | None = None,
    email: str | None = None,
    status_callback: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Update a Customer Profile. Set status='pending-review' to submit for vetting."""
    body: dict[str, Any] = {}
    if status is not None:
        body["Status"] = status
    if friendly_name is not None:
        body["FriendlyName"] = friendly_name
    if email is not None:
        body["Email"] = email
    if status_callback is not None:
        body["StatusCallback"] = status_callback

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/CustomerProfiles/{customer_profile_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


def list_customer_profiles(
    account_sid: str,
    auth_token: str,
    *,
    status: str | None = None,
    friendly_name: str | None = None,
    policy_sid: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """List Customer Profiles with optional filters."""
    params: dict[str, Any] = {}
    if status is not None:
        params["Status"] = status
    if friendly_name is not None:
        params["FriendlyName"] = friendly_name
    if policy_sid is not None:
        params["PolicySid"] = policy_sid

    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/CustomerProfiles",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params if params else None,
    )


# ---------------------------------------------------------------------------
# TrustProduct (SHAKEN/STIR registration per company)
# ---------------------------------------------------------------------------


def create_trust_product(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str,
    email: str,
    policy_sid: str,
    status_callback: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Create a Trust Product bundle (e.g. for SHAKEN/STIR)."""
    body: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Email": email,
        "PolicySid": policy_sid,
    }
    if status_callback is not None:
        body["StatusCallback"] = status_callback

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/TrustProducts",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


def get_trust_product(
    account_sid: str,
    auth_token: str,
    *,
    trust_product_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch a Trust Product by SID."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/TrustProducts/{trust_product_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_trust_product(
    account_sid: str,
    auth_token: str,
    *,
    trust_product_sid: str,
    status: str | None = None,
    friendly_name: str | None = None,
    email: str | None = None,
    status_callback: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Update a Trust Product. Set status='pending-review' to submit for vetting."""
    body: dict[str, Any] = {}
    if status is not None:
        body["Status"] = status
    if friendly_name is not None:
        body["FriendlyName"] = friendly_name
    if email is not None:
        body["Email"] = email
    if status_callback is not None:
        body["StatusCallback"] = status_callback

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/TrustProducts/{trust_product_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


# ---------------------------------------------------------------------------
# EndUser (business info + authorized representatives)
# ---------------------------------------------------------------------------


def create_end_user(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str,
    type: str,
    attributes: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Create an EndUser resource (business info or authorized representative)."""
    body: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Type": type,
    }
    if attributes is not None:
        body["Attributes"] = attributes

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/EndUsers",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


def get_end_user(
    account_sid: str,
    auth_token: str,
    *,
    end_user_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch an EndUser by SID."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/EndUsers/{end_user_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_end_user(
    account_sid: str,
    auth_token: str,
    *,
    end_user_sid: str,
    friendly_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Update an EndUser's friendly name or attributes."""
    body: dict[str, Any] = {}
    if friendly_name is not None:
        body["FriendlyName"] = friendly_name
    if attributes is not None:
        body["Attributes"] = attributes

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/EndUsers/{end_user_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


# ---------------------------------------------------------------------------
# SupportingDocument (address proof)
# ---------------------------------------------------------------------------


def create_supporting_document(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str,
    type: str,
    attributes: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Create a SupportingDocument (e.g. customer_profile_address)."""
    body: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Type": type,
    }
    if attributes is not None:
        body["Attributes"] = attributes

    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/SupportingDocuments",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload=body,
    )


def get_supporting_document(
    account_sid: str,
    auth_token: str,
    *,
    supporting_document_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch a SupportingDocument by SID."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/SupportingDocuments/{supporting_document_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# EntityAssignment (attach EndUsers/Documents to bundles)
# ---------------------------------------------------------------------------


def create_entity_assignment(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    object_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Attach an EndUser, SupportingDocument, or CustomerProfile to a bundle."""
    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/EntityAssignments",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload={"ObjectSid": object_sid},
    )


def list_entity_assignments(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """List EntityAssignments for a bundle."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/EntityAssignments",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# ChannelEndpointAssignment (attach phone numbers to bundles)
# ---------------------------------------------------------------------------


def create_channel_endpoint_assignment(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    channel_endpoint_type: str,
    channel_endpoint_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Attach a phone number to a bundle."""
    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/ChannelEndpointAssignments",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload={
            "ChannelEndpointType": channel_endpoint_type,
            "ChannelEndpointSid": channel_endpoint_sid,
        },
    )


def list_channel_endpoint_assignments(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """List ChannelEndpointAssignments for a bundle."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/ChannelEndpointAssignments",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Evaluation (pre-validation before submission)
# ---------------------------------------------------------------------------


def create_evaluation(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    policy_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Run a pre-validation evaluation against a bundle before submission."""
    return request_json(
        method="POST",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/Evaluations",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        json_payload={"PolicySid": policy_sid},
    )


def get_evaluation(
    account_sid: str,
    auth_token: str,
    *,
    bundle_sid: str,
    evaluation_sid: str,
    bundle_type: str = "CustomerProfiles",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch an Evaluation result by SID."""
    return request_json(
        method="GET",
        url=f"{TWILIO_TRUSTHUB_BASE}/v1/{bundle_type}/{bundle_sid}/Evaluations/{evaluation_sid}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )
