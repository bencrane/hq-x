"""Pydantic response models for the analytics router.

Built incrementally alongside the analytics endpoints. Each endpoint
gets its own response shape so consumers can rely on stable field names
even as the underlying queries evolve.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class _Window(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


# ── Reliability ─────────────────────────────────────────────────────────


class ReliabilityProvider(BaseModel):
    provider_slug: str
    events_total: int
    replays_total: int
    by_status: dict[str, int]


class ReliabilityTotals(BaseModel):
    events: int
    replays: int


class ReliabilityResponse(BaseModel):
    window: _Window
    providers: list[ReliabilityProvider]
    totals: ReliabilityTotals
    source: Literal["postgres"]


__all__ = [
    "ReliabilityProvider",
    "ReliabilityResponse",
    "ReliabilityTotals",
]
