"""Lightweight in-process metric counter.

No external metrics backend — this is a logging shim plus an in-memory
counter so SLO checks (signature reject rate, dead-letter rate, etc.) can
read aggregates within the lifetime of one process. Production observability
gets layered on top later.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict

logger = logging.getLogger("metrics")

_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)


def _tag_key(name: str, tags: dict[str, str]) -> str:
    if not tags:
        return name
    parts = "|".join(f"{k}={v}" for k, v in sorted(tags.items()))
    return f"{name}|{parts}"


def incr_metric(name: str, value: int = 1, **tags: object) -> None:
    str_tags = {k: str(v) for k, v in tags.items() if v is not None}
    key = _tag_key(name, str_tags)
    with _lock:
        _counters[key] += value
    logger.info(
        "metric name=%s value=%d %s",
        name,
        value,
        " ".join(f"{k}={v}" for k, v in str_tags.items()),
    )


def metrics_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset_metrics() -> None:
    """Test hook — never call from production code."""
    with _lock:
        _counters.clear()
