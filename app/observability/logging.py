"""Structured event logger.

`log_event(event, **fields)` emits a single INFO line with the event name and
the keyword fields rendered as `k=v` pairs. No JSON-line formatter wired in
yet — production logging stack will format these later. Keep call sites
clean so a future swap to a real structlog facade is mechanical.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("event")


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.log(level, "event=%s %s", event, rendered)
