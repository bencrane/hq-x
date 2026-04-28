from __future__ import annotations

from typing import Any

from app.providers.vapi._http import request


def create_assistant(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi assistant. POST /assistant"""
    return request("POST", "/assistant", api_key, json=config)


def get_assistant(api_key: str, assistant_id: str) -> dict[str, Any]:
    """Get a Vapi assistant. GET /assistant/{id}"""
    return request("GET", f"/assistant/{assistant_id}", api_key)


def update_assistant(api_key: str, assistant_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi assistant. PATCH /assistant/{id}"""
    return request("PATCH", f"/assistant/{assistant_id}", api_key, json=config)


def delete_assistant(api_key: str, assistant_id: str) -> None:
    """Delete a Vapi assistant. DELETE /assistant/{id}"""
    request("DELETE", f"/assistant/{assistant_id}", api_key)


def create_call(
    api_key: str,
    assistant_id: str,
    customer_number: str,
    phone_number_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Create an outbound call via Vapi. POST /call"""
    payload: dict[str, Any] = {
        "assistantId": assistant_id,
        "customer": {"number": customer_number},
        "phoneNumberId": phone_number_id,
    }
    if overrides:
        payload["assistantOverrides"] = overrides
    return request("POST", "/call", api_key, json=payload)


def get_call(api_key: str, call_id: str) -> dict[str, Any]:
    """Get a Vapi call. GET /call/{id}"""
    return request("GET", f"/call/{call_id}", api_key)


def list_calls(
    api_key: str,
    assistant_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List Vapi calls. GET /call"""
    params: dict[str, Any] = {"limit": limit}
    if assistant_id is not None:
        params["assistantId"] = assistant_id
    return request("GET", "/call", api_key, params=params)


def import_phone_number(
    api_key: str,
    provider: str,
    number: str,
    twilio_account_sid: str,
    twilio_auth_token: str,
) -> dict[str, Any]:
    """Import a phone number into Vapi. POST /phone-number"""
    payload: dict[str, Any] = {
        "provider": provider,
        "number": number,
        "twilioAccountSid": twilio_account_sid,
        "twilioAuthToken": twilio_auth_token,
    }
    return request("POST", "/phone-number", api_key, json=payload)


def get_phone_number(api_key: str, phone_number_id: str) -> dict[str, Any]:
    """Get a Vapi phone number. GET /phone-number/{id}"""
    return request("GET", f"/phone-number/{phone_number_id}", api_key)


def delete_phone_number(api_key: str, phone_number_id: str) -> None:
    """Delete a Vapi phone number. DELETE /phone-number/{id}"""
    request("DELETE", f"/phone-number/{phone_number_id}", api_key)
