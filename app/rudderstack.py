"""RudderStack write client (lazy singleton).

Mirrors the shape of ``app/clickhouse.py``:

* :func:`_is_configured` — both env vars set.
* :func:`_get_client` — lazy singleton init.
* :func:`track` — fire-and-forget. Never raises.
* :func:`flush` — call on app shutdown to drain the SDK's batch queue.

The SDK (``rudder-sdk-python``) is the canonical PyPI package
documented at
``api-reference-docs-new/rudderstack/sources/event-streams/sdks/rudderstack-python-sdk.md``.
We use ``rudderstack.analytics`` as the module-level singleton, so
"the client" here is a thin wrapper that:

1. Refuses to import the SDK or set ``write_key`` / ``dataPlaneUrl``
   when env vars are missing — important so dev runs without Doppler
   stay quiet (no broken event uploads spamming the log).
2. Caches a single import + initialization on first call.

Configured keys come from Doppler `hq-x/dev` and `hq-x/prd`:

* ``RUDDERSTACK_WRITE_KEY``
* ``RUDDERSTACK_DATA_PLANE_URL``
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# The module reference, populated lazily. Type as ``Any`` to avoid a
# hard dependency on rudderstack at import time of *this* module.
_client: Any | None = None
_init_failed: bool = False


def _is_configured() -> bool:
    return (
        settings.RUDDERSTACK_WRITE_KEY is not None
        and settings.RUDDERSTACK_DATA_PLANE_URL is not None
        and bool(settings.RUDDERSTACK_DATA_PLANE_URL.strip())
    )


def _get_client() -> Any | None:
    """Return the configured ``rudderstack.analytics`` module, or None.

    Lazy: the SDK is only imported on first use, so unconfigured
    environments never load it. Caches both success (module reference)
    and failure (returns None on subsequent calls without re-trying).
    """
    global _client, _init_failed
    if _client is not None:
        return _client
    if _init_failed:
        return None
    if not _is_configured():
        return None

    try:
        import rudderstack.analytics as rudder_analytics  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover — package is in pyproject
        logger.exception("rudderstack SDK import failed")
        _init_failed = True
        return None

    try:
        assert settings.RUDDERSTACK_WRITE_KEY is not None  # _is_configured()
        rudder_analytics.write_key = (
            settings.RUDDERSTACK_WRITE_KEY.get_secret_value()
        )
        rudder_analytics.dataPlaneUrl = settings.RUDDERSTACK_DATA_PLANE_URL
    except Exception:  # pragma: no cover
        logger.exception("rudderstack init failed")
        _init_failed = True
        return None

    _client = rudder_analytics
    return _client


def track(
    *,
    event_name: str,
    anonymous_id: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget ``track`` call.

    Never raises. When the SDK is unconfigured or unavailable, this is a
    no-op (the caller's log line is the only record of the event, which
    matches what was happening before this module existed).
    """
    client = _get_client()
    if client is None:
        return
    try:
        client.track(
            anonymous_id=anonymous_id,
            event=event_name,
            properties=dict(properties or {}),
        )
    except Exception:
        logger.warning(
            "rudderstack track failed",
            extra={"event": event_name, "anonymous_id": anonymous_id},
        )


def flush() -> None:
    """Drain the SDK's batch queue. Safe to call when unconfigured.

    Wired into ``app/main.py`` lifespan shutdown so an in-flight event
    isn't dropped on container exit.
    """
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:  # pragma: no cover
        logger.warning("rudderstack flush raised", exc_info=True)


def _reset_for_tests() -> None:
    """Reset the lazy singleton — used by the test suite."""
    global _client, _init_failed
    _client = None
    _init_failed = False


__all__ = [
    "flush",
    "track",
]
