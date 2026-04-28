"""ClickHouse HTTP client (no SDK).

Hand-rolled HTTP-only client for the analytics dual-write path. All writes
are fire-and-forget — call sites swallow exceptions and log. There is no
DLQ in Phase 1 (deferred per directive §10).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _is_configured() -> bool:
    return all([
        settings.CLICKHOUSE_URL,
        settings.CLICKHOUSE_USER,
        settings.CLICKHOUSE_PASSWORD,
    ])


def insert_row(table: str, row: dict[str, Any], *, timeout_seconds: float = 5.0) -> bool:
    """Insert a single row into a ClickHouse table via JSONEachRow.

    Returns True on success, False if ClickHouse is unconfigured or the write
    fails. Never raises — analytics is fire-and-forget.
    """
    if not _is_configured():
        return False

    url = f"{settings.CLICKHOUSE_URL.rstrip('/')}/?database={settings.CLICKHOUSE_DATABASE}"
    auth = (settings.CLICKHOUSE_USER or "", settings.CLICKHOUSE_PASSWORD.get_secret_value() if settings.CLICKHOUSE_PASSWORD else "")

    body = f"INSERT INTO {table} FORMAT JSONEachRow\n{json.dumps(row)}"
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, content=body, auth=auth)
        if response.status_code >= 400:
            logger.warning(
                "clickhouse insert failed",
                extra={"table": table, "status": response.status_code, "body": response.text[:300]},
            )
            return False
    except httpx.HTTPError as exc:
        logger.warning("clickhouse connectivity error", extra={"table": table, "error": str(exc)})
        return False
    return True
