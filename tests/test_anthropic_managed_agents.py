"""Unit tests for app.services.anthropic_managed_agents — the helper
parser + run_session loop. Network-touching paths are exercised via
test_agent_prompts_service.py (mocked at the function boundary)."""

from __future__ import annotations

import pytest

from app.services import anthropic_managed_agents as mags


def test_parse_events_concatenates_assistant_text_across_turns():
    events = [
        {
            "type": "user.message",
            "content": [{"type": "text", "text": "go"}],
        },
        {
            "type": "assistant.message",
            "content": [{"type": "text", "text": "first chunk."}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
        {
            "type": "tool.use",
            "id": "tool_1",
            "name": "dex_search",
            "input": {"query": "DAT carriers"},
        },
        {
            "type": "tool.result",
            "tool_use_id": "tool_1",
            "content": [{"type": "text", "text": "(15 rows)"}],
            "duration_ms": 320,
        },
        {
            "type": "assistant.message",
            "content": [{"type": "text", "text": " second chunk."}],
            "usage": {"output_tokens": 30},
            "stop_reason": "end_turn",
        },
    ]
    parsed = mags._parse_events(events)
    assert parsed["assistant_text"] == "first chunk. second chunk."
    assert parsed["stop_reason"] == "end_turn"
    # Tokens accumulate across turns.
    assert parsed["usage"]["output_tokens"] == 50
    assert parsed["usage"]["input_tokens"] == 100
    # Tool call captured + matched with its result.
    assert len(parsed["mcp_calls"]) == 1
    call = parsed["mcp_calls"][0]
    assert call["tool_name"] == "dex_search"
    assert call["result_preview"] is not None


def test_parse_events_ignores_unknown_blocks_gracefully():
    events = [
        {"type": "agent.start"},
        {
            "type": "assistant.message",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "thinking", "text": "(internal)"},
            ],
            "stop_reason": "end_turn",
        },
        {"type": "agent.finish"},
    ]
    parsed = mags._parse_events(events)
    assert parsed["assistant_text"] == "ok"
    assert parsed["mcp_calls"] == []


def test_events_show_terminal_detects_stop_reason():
    assert mags._events_show_terminal(
        [{"type": "assistant.message", "stop_reason": "end_turn"}]
    )
    assert mags._events_show_terminal(
        [{"type": "assistant.message", "status": "completed"}]
    )
    assert not mags._events_show_terminal(
        [{"type": "assistant.message", "content": []}]
    )
    assert not mags._events_show_terminal([])


def test_events_show_terminal_ignores_non_assistant_events():
    events = [
        {"type": "tool.use", "stop_reason": "end_turn"},  # wrong type
        {"type": "user.message", "stop_reason": "end_turn"},
    ]
    assert not mags._events_show_terminal(events)


def test_truncate_for_log_caps_long_strings():
    long_text = "x" * 2000
    out = mags._truncate_for_log(long_text, max_len=100)
    assert out is not None
    assert len(out) == 100
    assert out.endswith("…")


def test_truncate_for_log_serializes_non_string_payloads():
    out = mags._truncate_for_log({"key": "value", "n": 7})
    assert out is not None
    assert '"key"' in out and '"n"' in out


def test_run_session_handles_unconfigured_key(monkeypatch):
    # Stub the api-key resolver to mimic no-key state, and assert
    # ManagedAgentsNotConfiguredError surfaces from create_session.
    def raise_unconfigured():
        raise mags.ManagedAgentsNotConfiguredError(
            status_code=None, message="unconfigured"
        )

    monkeypatch.setattr(mags, "_api_key_or_raise", raise_unconfigured)

    import asyncio

    async def _run():
        with pytest.raises(mags.ManagedAgentsNotConfiguredError):
            await mags.create_session("agt_x")

    asyncio.run(_run())
