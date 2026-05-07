"""Centralized alert emission for Cluster 3.

Currently writes:
  1. business.cluster3_alerts row (always — durable audit trail).
  2. Telegram message (if TELEGRAM_BOT_TOKEN + TELEGRAM_OPERATOR_CHAT_ID
     are both configured; otherwise no-op + delivery_failures stamps
     'telegram_not_configured').
  3. logger.warning at info / warning, logger.error at critical.

The alert record is the source of truth — Telegram is a delivery
channel. If Telegram is down or unconfigured, the alert is still queryable
in the dashboard.

Severities:
  info     — informational, not actionable
  warning  — operator should look but no SLA
  critical — operator should act now (silent failure, dispatch broken,
             revenue at risk)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from uuid import UUID

import httpx
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection

logger = logging.getLogger(__name__)


Severity = Literal["info", "warning", "critical"]


_TELEGRAM_API = "https://api.telegram.org"


async def fire_alert(
    *,
    severity: Severity,
    source: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> UUID:
    """Emit an alert. Always returns the alert row id even if Telegram
    delivery fails — durable record-of-attempt is more valuable than
    blocking on the side-channel.
    """
    payload = payload or {}
    delivered: list[str] = ["log"]
    delivery_failures: list[dict[str, Any]] = []

    log_msg = f"[cluster3][{severity}] {source}: {summary}"
    if severity == "critical":
        logger.error(log_msg, extra={"alert_payload": payload})
    elif severity == "warning":
        logger.warning(log_msg, extra={"alert_payload": payload})
    else:
        logger.info(log_msg, extra={"alert_payload": payload})

    bot_token = _resolve_setting("TELEGRAM_BOT_TOKEN")
    chat_id = _resolve_setting("TELEGRAM_OPERATOR_CHAT_ID")
    if bot_token and chat_id:
        try:
            await _send_telegram(
                bot_token=bot_token,
                chat_id=chat_id,
                severity=severity,
                source=source,
                summary=summary,
                payload=payload,
            )
            delivered.append("telegram")
        except Exception as exc:  # noqa: BLE001 — never raise out of alert
            delivery_failures.append(
                {
                    "channel": "telegram",
                    "error": str(exc)[:500],
                }
            )
    else:
        delivery_failures.append(
            {"channel": "telegram", "error": "telegram_not_configured"}
        )

    return await _persist_alert(
        severity=severity,
        source=source,
        summary=summary,
        payload=payload,
        delivered=delivered,
        delivery_failures=delivery_failures,
    )


def _resolve_setting(name: str) -> str | None:
    value = getattr(settings, name, None)
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value) or None


async def _send_telegram(
    *,
    bot_token: str,
    chat_id: str,
    severity: Severity,
    source: str,
    summary: str,
    payload: dict[str, Any],
) -> None:
    icon = {"info": "i", "warning": "!", "critical": "X"}.get(severity, "i")
    text_lines = [f"[{icon} {severity.upper()}] {source}", summary]
    if payload:
        compact = json.dumps(payload, default=str, indent=2)[:1500]
        text_lines.extend(["", "```", compact, "```"])
    text = "\n".join(text_lines)

    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()


async def _persist_alert(
    *,
    severity: Severity,
    source: str,
    summary: str,
    payload: dict[str, Any],
    delivered: list[str],
    delivery_failures: list[dict[str, Any]],
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster3_alerts
                    (severity, source, summary, payload, delivered_to,
                     delivery_failures)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    severity,
                    source,
                    summary,
                    Jsonb(payload),
                    delivered,
                    Jsonb(delivery_failures),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


__all__ = ["fire_alert", "Severity"]
