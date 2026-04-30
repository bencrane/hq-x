"""Pydantic models for the customer-facing webhook subscription surface.

Standard SaaS webhook pattern: customer subscribes to event names (with
``*`` wildcard), we POST HMAC-signed payloads to their URL, retry with
exponential backoff on failure, dead-letter after 5 attempts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CustomerWebhookSubscriptionState = Literal[
    "active", "paused", "delivery_failing"
]

CustomerWebhookDeliveryStatus = Literal[
    "pending", "succeeded", "failed", "dead_lettered"
]


class CustomerWebhookSubscriptionCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    event_filter: list[str] = Field(min_length=1, max_length=64)
    brand_id: UUID | None = None

    model_config = {"extra": "forbid"}

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("url must be an http(s) URL")
        return v

    @field_validator("event_filter")
    @classmethod
    def _normalize_filters(cls, v: list[str]) -> list[str]:
        out = []
        for entry in v:
            entry = (entry or "").strip()
            if not entry:
                raise ValueError("event_filter entries must be non-empty")
            if len(entry) > 128:
                raise ValueError("event_filter entries must be <= 128 chars")
            out.append(entry)
        return out


class CustomerWebhookSubscriptionUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_filter: list[str] | None = Field(default=None, min_length=1, max_length=64)
    state: Literal["active", "paused"] | None = None

    model_config = {"extra": "forbid"}


class CustomerWebhookSubscriptionResponse(BaseModel):
    id: UUID
    organization_id: UUID
    brand_id: UUID | None = None
    url: str
    event_filter: list[str]
    state: CustomerWebhookSubscriptionState
    consecutive_failures: int = 0
    last_delivery_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CustomerWebhookSubscriptionWithSecretResponse(
    CustomerWebhookSubscriptionResponse
):
    """One-time response that surfaces the plaintext ``secret`` to the
    customer. Used on create + rotate-secret only."""

    secret: str


class CustomerWebhookDeliveryResponse(BaseModel):
    id: UUID
    subscription_id: UUID
    event_name: str
    event_payload: dict[str, Any]
    attempt: int
    status: CustomerWebhookDeliveryStatus
    response_status: int | None = None
    response_body: str | None = None
    attempted_at: datetime
    next_retry_at: datetime | None = None


__all__ = [
    "CustomerWebhookSubscriptionState",
    "CustomerWebhookDeliveryStatus",
    "CustomerWebhookSubscriptionCreate",
    "CustomerWebhookSubscriptionUpdate",
    "CustomerWebhookSubscriptionResponse",
    "CustomerWebhookSubscriptionWithSecretResponse",
    "CustomerWebhookDeliveryResponse",
]
