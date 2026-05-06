"""Minimal async Trigger.dev v3 Management API client for schedule lifecycle.

Used by jsearch_schedules.py: when an operator creates a JSearch schedule,
we register a corresponding Trigger.dev schedule entity here so Trigger
fires the cron on cadence. Same on delete.

Auth: TRIGGER_SECRET_KEY (Doppler hq-all/prd) — scopes to hq-x's Trigger
project, which is canonical for the monorepo.

Endpoints used:
  POST   https://api.trigger.dev/api/v1/schedules            create
  DELETE https://api.trigger.dev/api/v1/schedules/{id}       delete
"""

from __future__ import annotations

import os
from typing import Any

import httpx

TRIGGER_API_BASE = "https://api.trigger.dev"
DEFAULT_TIMEOUT_SEC = 30.0


class TriggerDevError(Exception):
    """Raised when the Trigger.dev Management API returns a non-2xx."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Trigger.dev API {status_code}: {body!r}")


def _api_key() -> str:
    key = os.environ.get("TRIGGER_SECRET_KEY")
    if not key:
        raise RuntimeError("TRIGGER_SECRET_KEY not configured on the server.")
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def create_schedule(
    *,
    task: str,
    cron: str,
    timezone: str = "UTC",
    external_id: str,
    deduplication_key: str | None = None,
) -> dict[str, Any]:
    """Register a Trigger.dev schedule. Returns response body (includes `id`)."""
    body: dict[str, Any] = {
        "task": task,
        "cron": cron,
        "timezone": timezone,
        "externalId": external_id,
    }
    if deduplication_key:
        body["deduplicationKey"] = deduplication_key

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SEC) as client:
        r = await client.post(
            f"{TRIGGER_API_BASE}/api/v1/schedules",
            headers=_headers(),
            json=body,
        )
    if r.status_code >= 400:
        try:
            err_body: Any = r.json()
        except Exception:
            err_body = r.text
        raise TriggerDevError(r.status_code, err_body)
    return r.json()


async def delete_schedule(trigger_schedule_id: str) -> None:
    """Delete a Trigger.dev schedule. Tolerates 404 (already deleted)."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SEC) as client:
        r = await client.delete(
            f"{TRIGGER_API_BASE}/api/v1/schedules/{trigger_schedule_id}",
            headers=_headers(),
        )
    if r.status_code == 404:
        return
    if r.status_code >= 400:
        try:
            err_body: Any = r.json()
        except Exception:
            err_body = r.text
        raise TriggerDevError(r.status_code, err_body)
