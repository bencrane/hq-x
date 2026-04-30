"""Pydantic models for business.activation_jobs.

Job rows back every async DMaaS operation:
  * dmaas_campaign_activation — POST /api/v1/dmaas/campaigns flow
  * step_activation — single-step activation outside the opinionated path
  * step_scheduled_activation — durable-sleep multi-step scheduling

Status transitions:
  queued -> running -> {succeeded, failed, cancelled}
  failed (>24h, no retry) -> dead_lettered  (set by reconciliation cron)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ActivationJobKind = Literal[
    "dmaas_campaign_activation",
    "step_activation",
    "step_scheduled_activation",
]

ActivationJobStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "dead_lettered",
]


class ActivationJobHistoryEntry(BaseModel):
    """One entry in the job.history array.

    `kind` describes what happened: 'transition', 'retry', 'cancellation',
    etc. `at` is ISO-8601 UTC. `detail` carries arbitrary structured data
    that should help an operator understand the line.
    """

    at: str
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ActivationJobResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID
    kind: ActivationJobKind
    status: ActivationJobStatus
    idempotency_key: str | None = None
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    history: list[ActivationJobHistoryEntry] = Field(default_factory=list)
    trigger_run_id: str | None = None
    attempts: int = 0
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dead_lettered_at: datetime | None = None


__all__ = [
    "ActivationJobKind",
    "ActivationJobStatus",
    "ActivationJobHistoryEntry",
    "ActivationJobResponse",
]
