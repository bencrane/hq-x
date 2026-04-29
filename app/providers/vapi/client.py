from __future__ import annotations

from typing import Any

from app.providers.vapi._http import request, request_multipart

# ---------------------------------------------------------------------------
# Assistants
# ---------------------------------------------------------------------------


def create_assistant(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi assistant. POST /assistant"""
    return request("POST", "/assistant", api_key, json=config)


def get_assistant(api_key: str, assistant_id: str) -> dict[str, Any]:
    """Get a Vapi assistant. GET /assistant/{id}"""
    return request("GET", f"/assistant/{assistant_id}", api_key)


def list_assistants(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi assistants. GET /assistant"""
    return request("GET", "/assistant", api_key, params={"limit": limit})


def update_assistant(api_key: str, assistant_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi assistant. PATCH /assistant/{id}"""
    return request("PATCH", f"/assistant/{assistant_id}", api_key, json=config)


def delete_assistant(api_key: str, assistant_id: str) -> None:
    """Delete a Vapi assistant. DELETE /assistant/{id}"""
    request("DELETE", f"/assistant/{assistant_id}", api_key)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


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


def update_call(api_key: str, call_id: str, *, name: str | None = None) -> dict[str, Any]:
    """Update a Vapi call. PATCH /call/{id} (Vapi spec only allows ``name``)."""
    payload: dict[str, Any] = {"name": name} if name is not None else {}
    return request("PATCH", f"/call/{call_id}", api_key, json=payload)


def delete_call(api_key: str, call_id: str) -> dict[str, Any] | None:
    """Delete a Vapi call. DELETE /call/{id}"""
    return request("DELETE", f"/call/{call_id}", api_key)


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------


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


def list_phone_numbers(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi phone numbers. GET /phone-number"""
    return request("GET", "/phone-number", api_key, params={"limit": limit})


def update_phone_number(
    api_key: str,
    phone_number_id: str,
    *,
    assistant_id: str | None = None,
    server_url: str | None = None,
    fallback_destination: dict[str, Any] | None = None,
    name: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Update a Vapi phone number. PATCH /phone-number/{id}

    Common fields are explicit; ``extra`` covers provider-specific or
    less-common fields (squadId, workflowId, hooks, smsEnabled, etc.).
    """
    payload: dict[str, Any] = {}
    if assistant_id is not None:
        payload["assistantId"] = assistant_id
    if server_url is not None:
        payload["server"] = {"url": server_url}
    if fallback_destination is not None:
        payload["fallbackDestination"] = fallback_destination
    if name is not None:
        payload["name"] = name
    payload.update(extra)
    return request("PATCH", f"/phone-number/{phone_number_id}", api_key, json=payload)


def delete_phone_number(api_key: str, phone_number_id: str) -> None:
    """Delete a Vapi phone number. DELETE /phone-number/{id}"""
    request("DELETE", f"/phone-number/{phone_number_id}", api_key)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def create_tool(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi tool. POST /tool"""
    return request("POST", "/tool", api_key, json=config)


def get_tool(api_key: str, tool_id: str) -> dict[str, Any]:
    """Get a Vapi tool. GET /tool/{id}"""
    return request("GET", f"/tool/{tool_id}", api_key)


def list_tools(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi tools. GET /tool"""
    return request("GET", "/tool", api_key, params={"limit": limit})


def update_tool(api_key: str, tool_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi tool. PATCH /tool/{id}"""
    return request("PATCH", f"/tool/{tool_id}", api_key, json=config)


def delete_tool(api_key: str, tool_id: str) -> None:
    """Delete a Vapi tool. DELETE /tool/{id}"""
    request("DELETE", f"/tool/{tool_id}", api_key)


# ---------------------------------------------------------------------------
# Squads
# ---------------------------------------------------------------------------


def create_squad(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi squad. POST /squad"""
    return request("POST", "/squad", api_key, json=config)


def get_squad(api_key: str, squad_id: str) -> dict[str, Any]:
    """Get a Vapi squad. GET /squad/{id}"""
    return request("GET", f"/squad/{squad_id}", api_key)


def list_squads(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi squads. GET /squad"""
    return request("GET", "/squad", api_key, params={"limit": limit})


def update_squad(api_key: str, squad_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi squad. PATCH /squad/{id}"""
    return request("PATCH", f"/squad/{squad_id}", api_key, json=config)


def delete_squad(api_key: str, squad_id: str) -> None:
    """Delete a Vapi squad. DELETE /squad/{id}"""
    request("DELETE", f"/squad/{squad_id}", api_key)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def create_campaign(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi campaign. POST /campaign"""
    return request("POST", "/campaign", api_key, json=config)


def get_campaign(api_key: str, campaign_id: str) -> dict[str, Any]:
    """Get a Vapi campaign. GET /campaign/{id}"""
    return request("GET", f"/campaign/{campaign_id}", api_key)


def list_campaigns(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi campaigns. GET /campaign"""
    return request("GET", "/campaign", api_key, params={"limit": limit})


def update_campaign(api_key: str, campaign_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi campaign. PATCH /campaign/{id}"""
    return request("PATCH", f"/campaign/{campaign_id}", api_key, json=config)


def delete_campaign(api_key: str, campaign_id: str) -> None:
    """Delete a Vapi campaign. DELETE /campaign/{id}"""
    request("DELETE", f"/campaign/{campaign_id}", api_key)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def create_file(
    api_key: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    """Upload a file to Vapi. POST /file (multipart/form-data)"""
    files = {"file": (filename, file_bytes, content_type)}
    return request_multipart("POST", "/file", api_key, files=files)


def get_file(api_key: str, file_id: str) -> dict[str, Any]:
    """Get a Vapi file. GET /file/{id}"""
    return request("GET", f"/file/{file_id}", api_key)


def list_files(api_key: str) -> list[dict[str, Any]]:
    """List Vapi files. GET /file"""
    return request("GET", "/file", api_key)


def update_file(api_key: str, file_id: str, *, name: str) -> dict[str, Any]:
    """Update a Vapi file (name only, per spec). PATCH /file/{id}"""
    return request("PATCH", f"/file/{file_id}", api_key, json={"name": name})


def delete_file(api_key: str, file_id: str) -> dict[str, Any] | None:
    """Delete a Vapi file. DELETE /file/{id}"""
    return request("DELETE", f"/file/{file_id}", api_key)


# ---------------------------------------------------------------------------
# Knowledge bases
# ---------------------------------------------------------------------------


def create_knowledge_base(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi knowledge base. POST /knowledge-base"""
    return request("POST", "/knowledge-base", api_key, json=config)


def get_knowledge_base(api_key: str, kb_id: str) -> dict[str, Any]:
    """Get a Vapi knowledge base. GET /knowledge-base/{id}"""
    return request("GET", f"/knowledge-base/{kb_id}", api_key)


def list_knowledge_bases(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi knowledge bases. GET /knowledge-base"""
    return request("GET", "/knowledge-base", api_key, params={"limit": limit})


def update_knowledge_base(
    api_key: str, kb_id: str, config: dict[str, Any],
) -> dict[str, Any]:
    """Update a Vapi knowledge base. PATCH /knowledge-base/{id}"""
    return request("PATCH", f"/knowledge-base/{kb_id}", api_key, json=config)


def delete_knowledge_base(api_key: str, kb_id: str) -> None:
    """Delete a Vapi knowledge base. DELETE /knowledge-base/{id}"""
    request("DELETE", f"/knowledge-base/{kb_id}", api_key)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def query_analytics(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Query Vapi analytics. POST /analytics"""
    return request("POST", "/analytics", api_key, json=config)


# ---------------------------------------------------------------------------
# Reporting / Insight
# ---------------------------------------------------------------------------


def create_insight(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create a Vapi insight. POST /reporting/insight"""
    return request("POST", "/reporting/insight", api_key, json=config)


def get_insight(api_key: str, insight_id: str) -> dict[str, Any]:
    """Get a Vapi insight. GET /reporting/insight/{id}"""
    return request("GET", f"/reporting/insight/{insight_id}", api_key)


def list_insights(api_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """List Vapi insights. GET /reporting/insight"""
    return request("GET", "/reporting/insight", api_key, params={"limit": limit})


def update_insight(api_key: str, insight_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Update a Vapi insight. PATCH /reporting/insight/{id}"""
    return request("PATCH", f"/reporting/insight/{insight_id}", api_key, json=config)


def delete_insight(api_key: str, insight_id: str) -> None:
    """Delete a Vapi insight. DELETE /reporting/insight/{id}"""
    request("DELETE", f"/reporting/insight/{insight_id}", api_key)


def preview_insight(api_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Preview a Vapi insight without saving. POST /reporting/insight/preview"""
    return request("POST", "/reporting/insight/preview", api_key, json=config)


def run_insight(
    api_key: str,
    insight_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a saved Vapi insight. POST /reporting/insight/{id}/run"""
    return request(
        "POST",
        f"/reporting/insight/{insight_id}/run",
        api_key,
        json=config if config is not None else {},
    )
