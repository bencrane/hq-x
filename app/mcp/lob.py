"""MCP server exposing Lob direct-mail tools as MCP tools — TEST MODE ONLY.

Mounted under `/mcp/lob` on the FastAPI app via FastMCP's `http_app()` (see
app/main.py). Managed agents connect via standard MCP HTTP transport; transport
auth is the shared MCP bearer (DMAAS_MCP_BEARER_TOKEN) at the mount boundary.

SAFETY — this server is hard-bound to `settings.LOB_API_KEY_TEST` and refuses
to run unless that key is a Lob *test* key (prefix `test_`). Lob test mode
accepts create calls and returns realistic objects but produces NO physical
mail and incurs NO charge. There is intentionally no path here to use a live
(`live_`) key — going live is a deliberate, out-of-band change, never something
an agent can do by toggling a flag. Tools return structured `{"error": ...}`
dicts on failure instead of raising.

Each tool is a thin wrapper around `app.providers.lob.client` (a synchronous
httpx client), dispatched via `asyncio.to_thread` so the event loop never
blocks on Lob I/O.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from app.config import settings
from app.providers.lob import client as lob

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="hq-x Lob (TEST mode)",
    instructions=(
        "Create and inspect Lob direct-mail pieces in TEST mode — postcards, "
        "letters, self-mailers — and verify US addresses. Every call uses Lob's "
        "test key, so nothing real is printed or mailed and nothing is charged; "
        "responses mirror the live API shape for end-to-end rehearsal. Addresses "
        "are objects like {name, address_line1, address_city, address_state, "
        "address_zip}. `front`/`back`/`file` accept an HTML string, a hosted URL, "
        "or a Lob template id (tmpl_...)."
    ),
)


def _test_key() -> str:
    """Return the Lob TEST key or raise — refuses anything not prefixed test_."""
    key = settings.LOB_API_KEY_TEST
    if not key:
        raise RuntimeError("LOB_API_KEY_TEST is not configured on the server")
    if not key.startswith("test_"):
        raise RuntimeError(
            "LOB_API_KEY_TEST is not a Lob test key (must start with 'test_'); "
            "refusing to send live, billable mail from the MCP"
        )
    return key


async def _call(
    method: str,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    try:
        key = _test_key()
    except RuntimeError as e:
        return {"error": "lob_test_key_unavailable", "message": str(e)}
    try:
        result = await asyncio.to_thread(
            lob._request_json,
            method=method,
            path=path,
            api_key=key,
            json_payload=json_payload,
            params=params,
            idempotency_key=idempotency_key,
        )
        return result if isinstance(result, dict) else {"result": result}
    except lob.LobProviderError as e:
        return {"error": "lob_api_error", "message": str(e)}
    except Exception as e:  # noqa: BLE001 — surface as data, never crash the session
        return {"error": "lob_call_failed", "message": str(e)}


@mcp.tool
async def verify_us_address(
    primary_line: str,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    secondary_line: str | None = None,
) -> dict[str, Any]:
    """Verify / standardize a US address via Lob. Provide either a full
    (city, state, zip_code) or just (primary_line, zip_code). Returns Lob's
    deliverability verdict + the corrected components."""
    payload: dict[str, Any] = {"primary_line": primary_line}
    if secondary_line:
        payload["secondary_line"] = secondary_line
    if city:
        payload["city"] = city
    if state:
        payload["state"] = state
    if zip_code:
        payload["zip_code"] = zip_code
    return await _call("POST", lob._EP_US_VERIFICATIONS, json_payload=payload)


@mcp.tool
async def create_postcard(
    to: dict[str, Any],
    from_address: dict[str, Any],
    front: str,
    back: str,
    size: str = "4x6",
    description: str | None = None,
    merge_variables: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a TEST-mode postcard. `to`/`from_address` are address objects (or
    Lob address ids). `front`/`back` are HTML, a hosted URL, or a template id.
    `size` is one of 4x6 | 6x9 | 6x11. Returns the created postcard object."""
    payload: dict[str, Any] = {
        "to": to,
        "from": from_address,
        "front": front,
        "back": back,
        "size": size,
    }
    if description:
        payload["description"] = description
    if merge_variables:
        payload["merge_variables"] = merge_variables
    return await _call(
        "POST", lob._EP_POSTCARDS, json_payload=payload, idempotency_key=idempotency_key
    )


@mcp.tool
async def create_letter(
    to: dict[str, Any],
    from_address: dict[str, Any],
    file: str,
    color: bool = True,
    address_placement: str = "top_first_page",
    description: str | None = None,
    merge_variables: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a TEST-mode letter. `file` is the letter body (HTML, hosted URL,
    or template id). `color` toggles color printing. `address_placement` is
    top_first_page | insert_blank_page. Returns the created letter object."""
    payload: dict[str, Any] = {
        "to": to,
        "from": from_address,
        "file": file,
        "color": color,
        "address_placement": address_placement,
    }
    if description:
        payload["description"] = description
    if merge_variables:
        payload["merge_variables"] = merge_variables
    return await _call(
        "POST", lob._EP_LETTERS, json_payload=payload, idempotency_key=idempotency_key
    )


@mcp.tool
async def create_self_mailer(
    to: dict[str, Any],
    from_address: dict[str, Any],
    inside: str,
    outside: str,
    size: str = "6x18_bifold",
    description: str | None = None,
    merge_variables: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a TEST-mode self-mailer. `inside`/`outside` are HTML, a hosted URL,
    or a template id. `size` is a Lob self-mailer size (e.g. 6x18_bifold,
    12x9_bifold). Returns the created self-mailer object."""
    payload: dict[str, Any] = {
        "to": to,
        "from": from_address,
        "inside": inside,
        "outside": outside,
        "size": size,
    }
    if description:
        payload["description"] = description
    if merge_variables:
        payload["merge_variables"] = merge_variables
    return await _call(
        "POST", lob._EP_SELF_MAILERS, json_payload=payload, idempotency_key=idempotency_key
    )


@mcp.tool
async def get_postcard(id: str) -> dict[str, Any]:
    """Fetch a previously created postcard by id (psc_...). Returns its status,
    expected delivery, thumbnails, and tracking events."""
    return await _call("GET", f"{lob._EP_POSTCARDS}/{id}")


@mcp.tool
async def list_postcards(limit: int = 10) -> dict[str, Any]:
    """List recent TEST-mode postcards (newest first). `limit` is clamped to
    1..100."""
    return await _call(
        "GET", lob._EP_POSTCARDS, params={"limit": max(1, min(limit, 100))}
    )
