"""Thin async wrapper around the Anthropic Python SDK.

This is the first hq-x → Anthropic call site. We keep it deliberately
small: one ``complete`` function, one ``AnthropicCallError`` class,
deterministic structured returns, and prompt caching applied to the
system prompt.

Caching strategy: callers pass either a plain string ``system`` or a
list of system content blocks. When a string is passed we wrap it as a
single text block with ``cache_control={"type": "ephemeral"}`` so the
prefix caches across iterations. Callers that need finer placement
(e.g. cache the static brand context, leave dynamic sections uncached)
pass the list shape directly.

The SDK is initialized lazily so importing this module never crashes a
test that has no Anthropic key set.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class AnthropicClientError(Exception):
    pass


class AnthropicNotConfiguredError(AnthropicClientError):
    pass


class AnthropicCallError(AnthropicClientError):
    """Raised when the Anthropic call returns an error response or the
    SDK itself raises. ``status_code`` mirrors the SDK exception's
    status_code when one is available; ``message`` is a short human
    string suitable for embedding in a 502/503 detail.
    """

    def __init__(self, *, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


_DEFAULT_MODEL = "claude-opus-4-7"
_DEFAULT_MAX_TOKENS = 8192


def _api_key_or_raise() -> str:
    secret = settings.ANTHROPIC_API_KEY
    if secret is None:
        raise AnthropicNotConfiguredError(
            "ANTHROPIC_API_KEY is not configured — cannot call Anthropic"
        )
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    return str(secret)


def _normalize_system(
    system: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize the ``system`` argument to a list of content blocks
    with cache_control on the trailing block so the static prefix
    caches across calls."""
    if isinstance(system, str):
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    if not system:
        return []
    # Caller supplied list shape — assume they put cache_control where
    # they want it. Don't second-guess.
    return list(system)


async def complete(
    *,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Single-shot completion against Anthropic's Messages API.

    Returns ``{text, usage, model, stop_reason}``. ``usage`` mirrors the
    SDK's usage object as a plain dict so callers can persist or surface
    cache-hit / token-count fields without depending on the SDK type.

    Notes on parameters:
      * ``model`` defaults to ``settings.ANTHROPIC_DEFAULT_MODEL`` which
        defaults to ``claude-opus-4-7``.
      * No ``temperature`` / ``top_p`` — both are removed on Opus 4.7
        (returns 400). Adaptive thinking is also off by default; the
        synthesizer doesn't need it for a structured-output task.
    """
    try:
        import anthropic  # local import — keeps import-time light
    except ImportError as exc:  # pragma: no cover — dep declared in pyproject
        raise AnthropicNotConfiguredError(
            "anthropic SDK not installed — `uv add anthropic`"
        ) from exc

    api_key = _api_key_or_raise()
    target_model = model or settings.ANTHROPIC_DEFAULT_MODEL or _DEFAULT_MODEL

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        response = await client.messages.create(
            model=target_model,
            max_tokens=max_tokens,
            system=_normalize_system(system),
            messages=messages,
        )
    except anthropic.APIStatusError as exc:
        raise AnthropicCallError(
            status_code=getattr(exc, "status_code", None),
            message=f"anthropic api error: {str(exc)[:500]}",
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise AnthropicCallError(
            status_code=None,
            message=f"anthropic connection error: {str(exc)[:500]}",
        ) from exc
    except anthropic.AnthropicError as exc:
        raise AnthropicCallError(
            status_code=None,
            message=f"anthropic sdk error: {str(exc)[:500]}",
        ) from exc

    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    text = "".join(text_parts)

    usage_obj = getattr(response, "usage", None)
    usage_dict: dict[str, Any] = {}
    if usage_obj is not None:
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = getattr(usage_obj, field, None)
            if value is not None:
                usage_dict[field] = value

    return {
        "text": text,
        "usage": usage_dict,
        "model": getattr(response, "model", target_model),
        "stop_reason": getattr(response, "stop_reason", None),
    }


__all__ = [
    "AnthropicClientError",
    "AnthropicNotConfiguredError",
    "AnthropicCallError",
    "complete",
]
