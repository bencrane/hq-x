"""Thin async HTTP client for the Anthropic Managed Agents API.

This is the runtime hq-x uses to invoke MAGS-registered agents in the
post-payment GTM pipeline. Mirrors managed-agents-x/app/anthropic_client.py
shape and adds:

* `update_agent_system_prompt` — wraps POST /v1/agents/{id} with the
  system field. Used by `agent_prompts.activate_prompt` to push a new
  prompt; the snapshot-then-overwrite invariant lives in that caller.
* `run_session` — the full one-shot session lifecycle: open a session,
  post one user message, await the agent's terminal assistant turn,
  return the parsed text plus the trace material that goes into
  business.gtm_subagent_runs (request ids, mcp call summary, usage).

The Anthropic Managed Agents events surface is documented as an
events stream — POSTing `user.message` triggers the agent's turn, and
the agent emits `assistant.message`, `tool.use`, `tool.result`, and a
final terminal event. We block on a poll loop against
GET /v1/sessions/{id}/events with a since-cursor until we see a
terminal-status event or a `stop_reason` on the latest assistant turn.
The loop budget is bounded by a wall-clock cap so a stuck session
doesn't hang the parent gtm pipeline orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
BETA_HEADER = "managed-agents-2026-04-01"


class ManagedAgentsError(Exception):
    """Raised on any non-2xx response or unexpected payload shape."""

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
    pass


def _api_key_or_raise() -> str:
    secret = settings.ANTHROPIC_MANAGED_AGENTS_API_KEY
    if secret is None:
        raise ManagedAgentsNotConfiguredError(
            status_code=None,
            message=(
                "ANTHROPIC_MANAGED_AGENTS_API_KEY is not configured — "
                "cannot call the Managed Agents API"
            ),
        )
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    return str(secret)


def _headers() -> dict[str, str]:
    return {
        "x-api-key": _api_key_or_raise(),
        "anthropic-version": API_VERSION,
        "anthropic-beta": BETA_HEADER,
        "content-type": "application/json",
    }


def _maybe_raise(resp: httpx.Response, op: str) -> None:
    if resp.status_code < 400:
        return
    body = resp.text
    raise ManagedAgentsError(
        status_code=resp.status_code,
        message=f"{op} failed: HTTP {resp.status_code}",
        response_body=body[:4000] if body else None,
    )


# ---------------------------------------------------------------------------
# Agent CRUD (read + system-prompt update only — registration lives in MAGS)
# ---------------------------------------------------------------------------


async def get_agent(agent_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.get(f"/v1/agents/{agent_id}", headers=_headers())
        _maybe_raise(resp, f"get_agent({agent_id})")
        return resp.json()


async def update_agent_system_prompt(
    agent_id: str,
    system_prompt: str,
) -> dict[str, Any]:
    """POST /v1/agents/{id} with the new system prompt only.

    Anthropic's POST is destructive on the fields supplied — fields not
    present in the body are left alone. We only ever push `system`,
    leaving model / tools / mcp_servers as-is. To change those, re-run
    the agent's setup script in managed-agents-x and re-register the
    new id in business.gtm_agent_registry.
    """
    body = {"system": system_prompt}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            f"/v1/agents/{agent_id}",
            headers=_headers(),
            json=body,
        )
        _maybe_raise(resp, f"update_agent_system_prompt({agent_id})")
        return resp.json()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def create_session(
    agent_id: str,
    *,
    vault_ids: list[str] | None = None,
    environment_id: str | None = None,
    title: str | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST /v1/sessions — opens a session against the agent.

    Defaults vault_ids and environment_id from settings if the caller
    doesn't pass them. Both default to the same constants
    managed-agents-x's setup scripts use, so a session opened here
    sees the same MCP credentials that an operator-driven session
    would.
    """
    body: dict[str, Any] = {"agent": agent_id}
    body["environment_id"] = (
        environment_id or settings.ANTHROPIC_MAGS_DEFAULT_ENVIRONMENT_ID
    )
    body["vault_ids"] = vault_ids or [
        settings.ANTHROPIC_MAGS_DEFAULT_VAULT_ID
    ]
    if title:
        body["title"] = title
    if metadata:
        body["metadata"] = metadata
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        resp = await client.post(
            "/v1/sessions",
            headers=_headers(),
            json=body,
        )
        _maybe_raise(resp, "create_session")
        return resp.json()


async def post_user_message(
    session_id: str,
    text: str,
) -> dict[str, Any]:
    body = {
        "events": [
            {
                "type": "user.message",
                "content": [{"type": "text", "text": text}],
            }
        ]
    }
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            json=body,
        )
        _maybe_raise(resp, f"post_user_message({session_id})")
        return resp.json()


async def list_session_events(
    session_id: str,
    *,
    after_cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """GET /v1/sessions/{id}/events — page forward via after_cursor."""
    params: dict[str, str | int] = {"limit": limit}
    if after_cursor:
        params["after"] = after_cursor
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        resp = await client.get(
            f"/v1/sessions/{session_id}/events",
            headers=_headers(),
            params=params,
        )
        _maybe_raise(resp, f"list_session_events({session_id})")
        return resp.json()


async def get_session(session_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.get(
            f"/v1/sessions/{session_id}",
            headers=_headers(),
        )
        _maybe_raise(resp, f"get_session({session_id})")
        return resp.json()


# ---------------------------------------------------------------------------
# Run-session — the full one-shot lifecycle
# ---------------------------------------------------------------------------


_TERMINAL_SESSION_STATUSES = {"completed", "failed", "cancelled", "stopped"}


async def run_session(
    *,
    agent_id: str,
    user_message: str,
    vault_ids: list[str] | None = None,
    environment_id: str | None = None,
    title: str | None = None,
    metadata: dict[str, str] | None = None,
    poll_interval_seconds: float = 1.5,
    wall_clock_timeout_seconds: float = 540.0,
) -> dict[str, Any]:
    """One-shot: open session → post user message → await terminal turn.

    Returns:

      ``{
          "session_id": str,
          "assistant_text": str,
          "events": list[dict],          # full event log captured during the run
          "request_ids": list[str],       # anthropic-request-id values seen
          "mcp_calls": list[dict],        # parsed mcp tool-call summaries
          "usage": dict,                  # input/output/cache tokens if surfaced
          "stop_reason": str | None,
          "terminal_status": str,         # 'completed' | 'timed_out' | ...
      }``

    The poll loop is bounded by ``wall_clock_timeout_seconds`` (default
    9 min — within the 10-min FastAPI timeout used by the
    /internal/gtm/run-step caller). On timeout we still return what we
    have plus terminal_status='timed_out' so the caller can record a
    failed run with the partial trace.
    """

    session = await create_session(
        agent_id,
        vault_ids=vault_ids,
        environment_id=environment_id,
        title=title,
        metadata=metadata,
    )
    session_id = session.get("id")
    if not session_id:
        raise ManagedAgentsError(
            status_code=None,
            message=(
                "create_session response missing 'id' field; "
                f"keys={list(session.keys())[:10]}"
            ),
        )

    post_resp = await post_user_message(session_id, user_message)

    deadline = asyncio.get_event_loop().time() + wall_clock_timeout_seconds
    after_cursor: str | None = None
    collected_events: list[dict[str, Any]] = []

    # Many MAGS responses include the full agent turn in the POST body
    # itself when the agent finishes within the request window. Capture
    # whatever the POST returned first so we don't miss events.
    if isinstance(post_resp, dict):
        for ev in post_resp.get("events", []) or []:
            collected_events.append(ev)
        if post_resp.get("next_cursor"):
            after_cursor = post_resp["next_cursor"]

    terminal_status = "running"
    last_session_status: str | None = None
    while True:
        # Check the session status first — short-circuits the poll if the
        # agent finished without us catching a terminal event in the
        # event stream alone.
        try:
            sess = await get_session(session_id)
        except ManagedAgentsError as exc:
            logger.warning(
                "get_session(%s) failed mid-run: %s",
                session_id, exc.message,
            )
            sess = {}
        sess_status = (sess or {}).get("status")
        if sess_status:
            last_session_status = sess_status
        if sess_status in _TERMINAL_SESSION_STATUSES:
            terminal_status = sess_status
            # Drain any remaining events before exiting.
            try:
                tail = await list_session_events(
                    session_id, after_cursor=after_cursor, limit=200,
                )
                for ev in tail.get("events", []) or []:
                    collected_events.append(ev)
            except ManagedAgentsError:
                pass
            break

        # Poll forward through events and break if we observe a terminal
        # assistant turn even when the session-status check hasn't
        # caught up yet.
        try:
            page = await list_session_events(
                session_id, after_cursor=after_cursor, limit=200,
            )
        except ManagedAgentsError as exc:
            # Treat list-failures as transient; loop will retry until
            # the deadline.
            logger.warning(
                "list_session_events(%s) failed: %s",
                session_id, exc.message,
            )
            page = {"events": []}
        new_events = page.get("events", []) or []
        for ev in new_events:
            collected_events.append(ev)
        next_cursor = page.get("next_cursor")
        if next_cursor:
            after_cursor = next_cursor

        if _events_show_terminal(collected_events):
            terminal_status = last_session_status or "completed"
            break

        if asyncio.get_event_loop().time() >= deadline:
            terminal_status = "timed_out"
            break

        await asyncio.sleep(poll_interval_seconds)

    parsed = _parse_events(collected_events)
    return {
        "session_id": session_id,
        "assistant_text": parsed["assistant_text"],
        "events": collected_events,
        "request_ids": parsed["request_ids"],
        "mcp_calls": parsed["mcp_calls"],
        "usage": parsed["usage"],
        "stop_reason": parsed["stop_reason"],
        "terminal_status": terminal_status,
    }


# ---------------------------------------------------------------------------
# Event parsing helpers
# ---------------------------------------------------------------------------


def _events_show_terminal(events: list[dict[str, Any]]) -> bool:
    """Heuristic: any assistant.message event with a stop_reason set
    means the agent finished its current turn."""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "assistant.message":
            continue
        if ev.get("stop_reason"):
            return True
        # Some implementations use status=completed on the event itself.
        if ev.get("status") in {"completed", "stop"}:
            return True
    return False


def _parse_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract assistant text + structured trace from the event log.

    The Managed Agents events surface returns dicts with at least:
      * type: 'user.message' | 'assistant.message' | 'tool.use' |
              'tool.result' | 'agent.error' | ...
      * content: list of {type, text, ...} blocks
      * usage / stop_reason / id / request_id when available

    This parser is intentionally tolerant: unknown fields are ignored,
    text is concatenated across all assistant.message events (the
    last-turn text dominates because it lands last). Tool-call summary
    captures the tool name + a truncated args/result preview so the
    debug UI has something to render without rebuilding the full
    payload.
    """
    text_chunks: list[str] = []
    request_ids: list[str] = []
    mcp_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    stop_reason: str | None = None

    pending_tool_uses: dict[str, dict[str, Any]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        rid = ev.get("request_id") or ev.get("anthropic_request_id")
        if rid and rid not in request_ids:
            request_ids.append(rid)

        if t == "assistant.message":
            for block in ev.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    txt = block.get("text") or ""
                    if txt:
                        text_chunks.append(txt)
            ev_usage = ev.get("usage")
            if isinstance(ev_usage, dict):
                # accumulate token totals across turns
                for k, v in ev_usage.items():
                    if isinstance(v, int):
                        usage[k] = (usage.get(k) or 0) + v
                    else:
                        usage[k] = v
            sr = ev.get("stop_reason")
            if sr:
                stop_reason = sr

        elif t == "tool.use":
            tool_id = ev.get("id") or ev.get("tool_use_id")
            tool_name = ev.get("name") or "(unknown)"
            args_preview = _truncate_for_log(ev.get("input"))
            entry = {
                "id": tool_id,
                "tool_name": tool_name,
                "args_preview": args_preview,
                "result_preview": None,
                "ms": ev.get("duration_ms"),
            }
            if tool_id:
                pending_tool_uses[tool_id] = entry
            mcp_calls.append(entry)

        elif t == "tool.result":
            tool_id = ev.get("tool_use_id") or ev.get("id")
            preview = _truncate_for_log(ev.get("content"))
            if tool_id and tool_id in pending_tool_uses:
                pending_tool_uses[tool_id]["result_preview"] = preview
                if ev.get("duration_ms") is not None:
                    pending_tool_uses[tool_id]["ms"] = ev["duration_ms"]
            else:
                # Unmatched result — record standalone.
                mcp_calls.append({
                    "id": tool_id,
                    "tool_name": "(orphan_result)",
                    "args_preview": None,
                    "result_preview": preview,
                    "ms": ev.get("duration_ms"),
                })

    return {
        "assistant_text": "".join(text_chunks),
        "request_ids": request_ids,
        "mcp_calls": mcp_calls,
        "usage": usage,
        "stop_reason": stop_reason,
    }


def _truncate_for_log(value: Any, *, max_len: int = 800) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else _safe_repr(value)
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _safe_repr(value: Any) -> str:
    try:
        import json
        return json.dumps(value, default=str)[:8000]
    except Exception:  # pragma: no cover — defensive
        return repr(value)[:8000]


__all__ = [
    "ManagedAgentsError",
    "ManagedAgentsNotConfiguredError",
    "get_agent",
    "update_agent_system_prompt",
    "create_session",
    "post_user_message",
    "list_session_events",
    "get_session",
    "run_session",
]
