"""Lifecycle wrapper for the `gtm-agent` Managed Agents sessions.

Mints sessions against the gtm-agent + gtm-env provisioned by
``scripts/managed_agents/provision.py``, exposes the user-event surface,
and pipes the raw SSE byte stream straight through to FastAPI's
``StreamingResponse`` so the platform-api → platform-app relay does
no JSON parsing in the hot path.

Distinct from ``app/services/anthropic_managed_agents.py`` (the legacy
poll-based service that drives the 10-agent ``business.gtm_agent_registry``
pipeline). Both use the same beta header and the same
``ANTHROPIC_MANAGED_AGENTS_API_KEY`` secret, but this module is the new
v1 lane: SSE streaming + agent_runs ledger + single shared `gtm-agent`.

Transport: ``httpx.AsyncClient`` everywhere — the official ``anthropic``
SDK is installed (used by the provisioner) but mixing the SDK's stream
iterator with FastAPI's StreamingResponse adds a parse/serialize round-
trip that defeats the point of a byte-pipe. Raw httpx preserves the
``text/event-stream`` payload verbatim.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
BETA_HEADER = "managed-agents-2026-04-01"

# Generous read timeout for the streaming endpoint — the agent may sit on
# a long tool call before emitting its next event. None disables the read
# timeout entirely; connect/write/pool stay bounded.
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=None)
_RPC_TIMEOUT = httpx.Timeout(60.0)


class ManagedAgentsError(Exception):
    def __init__(
        self,
        *,
        status_code: int | None,
        message: str,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.response_body = response_body


class ManagedAgentsNotConfiguredError(ManagedAgentsError):
    """Raised when a required secret / ID is missing from Doppler."""


def _api_key() -> str:
    secret = settings.ANTHROPIC_MANAGED_AGENTS_API_KEY
    if secret is None:
        raise ManagedAgentsNotConfiguredError(
            status_code=None,
            message="ANTHROPIC_MANAGED_AGENTS_API_KEY is not configured",
        )
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)


def _agent_id() -> str:
    if not settings.MANAGED_AGENT_ID_GTM:
        raise ManagedAgentsNotConfiguredError(
            status_code=None,
            message="MANAGED_AGENT_ID_GTM is not configured (Doppler)",
        )
    return settings.MANAGED_AGENT_ID_GTM


def _environment_id() -> str:
    if not settings.MANAGED_ENVIRONMENT_ID_GTM:
        raise ManagedAgentsNotConfiguredError(
            status_code=None,
            message="MANAGED_ENVIRONMENT_ID_GTM is not configured (Doppler)",
        )
    return settings.MANAGED_ENVIRONMENT_ID_GTM


def _headers(*, accept_sse: bool = False) -> dict[str, str]:
    hdrs = {
        "x-api-key": _api_key(),
        "anthropic-version": API_VERSION,
        "anthropic-beta": BETA_HEADER,
    }
    if accept_sse:
        hdrs["Accept"] = "text/event-stream"
    else:
        hdrs["content-type"] = "application/json"
    return hdrs


def _raise_for_status(resp: httpx.Response, op: str) -> None:
    if resp.status_code < 400:
        return
    body = resp.text
    raise ManagedAgentsError(
        status_code=resp.status_code,
        message=f"{op} failed: HTTP {resp.status_code}",
        response_body=body[:4000] if body else None,
    )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _default_vault_ids() -> list[str]:
    """Auto-injected vault list for every gtm-agent session.

    Currently the polaris vault — the gtm-agent's `polaris` MCP server
    needs the static_bearer credential to authenticate against the
    deployed polaris-mcp service. If MANAGED_VAULT_ID_POLARIS is unset
    (Stage 5 not yet provisioned, or local-dev), we skip injection and
    the agent's polaris tool calls will fail at the MCP transport — a
    correct failure mode (loud, attributable).
    """
    out: list[str] = []
    if settings.MANAGED_VAULT_ID_POLARIS:
        out.append(settings.MANAGED_VAULT_ID_POLARIS)
    return out


async def mint_session(
    initial_message: str,
    *,
    vault_ids: list[str] | None = None,
    title: str | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Open a session against the gtm-agent + gtm-env, send the first
    user.message, and return the session object.

    Race-condition note: the docs say "Only events emitted after the
    stream is opened are delivered." For the platform-api → platform-app
    flow, the frontend opens the stream BEFORE sending its first
    follow-up event, and uses the GET /events history endpoint to
    backfill anything emitted before the stream attached. Callers that
    don't backfill will miss the agent's first turn.

    Vault injection: callers can pass explicit ``vault_ids`` to override;
    when omitted, ``_default_vault_ids()`` is used. This auto-injects the
    polaris vault so the gtm-agent can authenticate its `polaris` MCP
    server out of the box (Stage 5+).
    """
    resolved_vault_ids = vault_ids if vault_ids is not None else _default_vault_ids()

    body: dict[str, Any] = {
        "agent": _agent_id(),
        "environment_id": _environment_id(),
    }
    if resolved_vault_ids:
        body["vault_ids"] = resolved_vault_ids
    if title:
        body["title"] = title
    if metadata:
        body["metadata"] = metadata

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        sess_resp = await client.post("/v1/sessions", headers=_headers(), json=body)
        _raise_for_status(sess_resp, "mint_session.create")
        session = sess_resp.json()

        session_id = session.get("id")
        if not session_id:
            raise ManagedAgentsError(
                status_code=None,
                message=f"create_session response missing 'id'; keys={list(session.keys())[:10]}",
            )

        # Send the initial user.message immediately. If this fails after
        # session creation, the session is orphaned but harmless — caller
        # can either delete it or just let it idle out.
        msg_body = {
            "events": [
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": initial_message}],
                }
            ]
        }
        msg_resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            json=msg_body,
        )
        _raise_for_status(msg_resp, f"mint_session.post_user_message({session_id})")

    return session


async def send_user_message(session_id: str, text: str) -> dict[str, Any]:
    """Append a single user.message event."""
    body = {
        "events": [
            {
                "type": "user.message",
                "content": [{"type": "text", "text": text}],
            }
        ]
    }
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            json=body,
        )
        _raise_for_status(resp, f"send_user_message({session_id})")
        return resp.json()


async def send_interrupt(session_id: str) -> dict[str, Any]:
    """Stop the agent mid-execution. Send a follow-up user.message to
    redirect."""
    body = {"events": [{"type": "user.interrupt"}]}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            json=body,
        )
        _raise_for_status(resp, f"send_interrupt({session_id})")
        return resp.json()


async def send_events(session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Send an arbitrary list of user-domain events. Lets the router
    forward steer / tool_confirmation / custom_tool_result bodies verbatim
    without growing one wrapper per event type."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            json={"events": events},
        )
        _raise_for_status(resp, f"send_events({session_id})")
        return resp.json()


async def list_events(
    session_id: str,
    *,
    after: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """GET /v1/sessions/{id}/events — page forward with `after`.

    Anthropic returns events under either ``"data"`` (current Managed
    Agents schema) or ``"events"`` (older shape). Normalize so callers
    always see ``"data"``.
    """
    params: dict[str, str | int] = {"limit": limit}
    if after:
        params["after"] = after
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        resp = await client.get(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            params=params,
        )
        _raise_for_status(resp, f"list_events({session_id})")
        body = resp.json()
        if isinstance(body, dict) and "data" not in body and "events" in body:
            body = {**body, "data": body["events"]}
        return body


async def retrieve_session(session_id: str) -> dict[str, Any]:
    """GET /v1/sessions/{id} — returns status + cumulative usage."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as client:
        resp = await client.get(
            f"/v1/sessions/{session_id}",
            headers=_headers(),
        )
        _raise_for_status(resp, f"retrieve_session({session_id})")
        return resp.json()


PRESENT_RESULT_TOOL_NAME = "present_result"


async def _ack_present_results(
    client: httpx.AsyncClient,
    session_id: str,
    event_ids: list[str],
) -> None:
    """Fire ``user.custom_tool_result`` ack events for present_result calls.

    The agent uses the ack purely as a "received" signal — the operator
    sees the result in the UI immediately. Failure logs but does not
    raise: a missed ack leaves the session sitting in requires_action,
    and the operator can manually ack via the UI as a last resort.
    """
    events = [
        {
            "type": "user.custom_tool_result",
            "custom_tool_use_id": eid,
            "content": [{"type": "text", "text": "ok"}],
        }
        for eid in event_ids
    ]
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        headers=_headers(),
        json={"events": events},
    )
    if resp.status_code >= 400:
        logger.warning(
            "auto-ack present_result failed: session=%s event_ids=%s status=%d body=%s",
            session_id, event_ids, resp.status_code, resp.text[:500],
        )


async def stream_events(session_id: str) -> AsyncIterator[bytes]:
    """Raw byte-pipe over Anthropic's SSE stream.

    Yields upstream chunks verbatim — no parsing, no transformation. For
    the agent-runs router we use ``stream_events_with_autoack`` so the
    agent's ``present_result`` custom-tool calls don't stall the stream
    waiting for an acknowledgment the operator never needs to give.
    ``stream_events`` remains the lowest-level primitive for callers
    that want raw access (debug tooling, future agent shapes that don't
    use present_result).

    NB: stream endpoint is ``/events/stream`` (the bare ``/stream`` 404s);
    ``?beta=true`` is required despite the beta header — mirror the
    SDK's path template (``anthropic.beta.sessions.events.stream``).
    """
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_STREAM_TIMEOUT) as client:
        async with client.stream(
            "GET",
            f"/v1/sessions/{session_id}/events/stream",
            params={"beta": "true"},
            headers=_headers(accept_sse=True),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise ManagedAgentsError(
                    status_code=response.status_code,
                    message=f"stream_events({session_id}) failed: HTTP {response.status_code}",
                    response_body=body[:4000],
                )
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk


async def stream_events_with_autoack(session_id: str) -> AsyncIterator[bytes]:
    """Byte-pipe with an out-of-band SSE parser that auto-acks ``present_result``.

    Yields upstream bytes downstream verbatim — the SSE wire shape is
    preserved exactly, including event IDs, comments, retry hints, and
    whatever fields Anthropic adds in future schema versions. The
    out-of-band tap parses complete ``data: {json}`` frames as a side
    effect and, when it sees:

      1. ``agent.custom_tool_use`` with ``name="present_result"`` → tracks
         the event_id in an in-process set.
      2. ``session.status_idle`` with ``stop_reason.type="requires_action"``
         whose ``event_ids`` intersect that set → POSTs
         ``user.custom_tool_result`` with content ``[{"type":"text","text":"ok"}]``
         for each matching event_id.

    Other ``agent.tool_use`` / generic ``requires_action`` blockers
    (permission policies, custom tools other than present_result) are
    NEVER auto-acked — those are user-decision events that the operator's
    UI surfaces as confirm/deny prompts.

    The frontend may briefly observe a ``requires_action`` frame whose
    ``event_ids`` list contains a present_result id; it should skip
    rendering a confirm UI for any event_id that came from a prior
    ``agent.custom_tool_use{name:"present_result"}`` event (which it
    already tracks for result-card dispatch).
    """
    pending_present_result_ids: set[str] = set()
    line_buffer = bytearray()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_RPC_TIMEOUT) as ack_client:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=_STREAM_TIMEOUT) as stream_client:
            async with stream_client.stream(
                "GET",
                f"/v1/sessions/{session_id}/events/stream",
                params={"beta": "true"},
                headers=_headers(accept_sse=True),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise ManagedAgentsError(
                        status_code=response.status_code,
                        message=(
                            f"stream_events_with_autoack({session_id}) failed: "
                            f"HTTP {response.status_code}"
                        ),
                        response_body=body[:4000],
                    )

                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue

                    # 1) Yield bytes downstream UNCHANGED — preserves wire shape.
                    yield chunk

                    # 2) Out-of-band parser: accumulate, split on \n, scan
                    #    `data: {json}` lines for present_result triggers.
                    line_buffer.extend(chunk)
                    while True:
                        nl = line_buffer.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(line_buffer[:nl])
                        del line_buffer[: nl + 1]

                        if not line.startswith(b"data: "):
                            continue

                        try:
                            ev = json.loads(line[len(b"data: "):].decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(ev, dict):
                            continue

                        ev_type = ev.get("type")

                        if (
                            ev_type == "agent.custom_tool_use"
                            and ev.get("name") == PRESENT_RESULT_TOOL_NAME
                        ):
                            ev_id = ev.get("id")
                            if ev_id:
                                pending_present_result_ids.add(ev_id)
                            continue

                        if ev_type == "session.status_idle":
                            stop = ev.get("stop_reason") or {}
                            if stop.get("type") != "requires_action":
                                continue
                            all_event_ids = list(stop.get("event_ids") or [])
                            ours = [
                                eid for eid in all_event_ids
                                if eid in pending_present_result_ids
                            ]
                            if not ours:
                                # Mixed requires_action with no present_result —
                                # leave for user confirm via the UI.
                                continue
                            # Ack inline. While in requires_action the session
                            # emits no further events, so awaiting the ack
                            # here delays nothing downstream.
                            await _ack_present_results(ack_client, session_id, ours)
                            for eid in ours:
                                pending_present_result_ids.discard(eid)


__all__ = [
    "ManagedAgentsError",
    "ManagedAgentsNotConfiguredError",
    "mint_session",
    "send_user_message",
    "send_interrupt",
    "send_events",
    "list_events",
    "retrieve_session",
    "stream_events",
    "stream_events_with_autoack",
]
