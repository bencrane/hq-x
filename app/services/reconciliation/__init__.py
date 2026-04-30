"""Reconciliation services — daily background sweeps that reconcile our
DB with provider state, catching dropped webhooks and stale jobs.

Per directive §1.7, reconciliation is read-only-ish: we read from
provider APIs and *fill gaps* in our DB, never mutating provider
state. Each task is feature-flag-gated so a single noisy reconciler
can be disabled via Doppler without a deploy.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReconciliationResult(BaseModel):
    """Common return shape for every reconciliation cron.

    `rows_scanned`  - how many candidate rows we looked at
    `rows_touched`  - how many we mutated (filled gaps, transitioned)
    `drift_found`   - rows where provider state diverged from ours
    `details`       - free-form structured data for operator inspection
    `enabled`       - false when the feature flag was off (no-op tick)
    """

    enabled: bool = True
    rows_scanned: int = 0
    rows_touched: int = 0
    drift_found: int = 0
    details: list[dict[str, Any]] = Field(default_factory=list)

    def add_drift(self, **kwargs: Any) -> None:
        self.drift_found += 1
        self.details.append(kwargs)


__all__ = ["ReconciliationResult"]
