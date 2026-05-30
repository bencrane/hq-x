"""Landing-zone writers for bulk_ingest payloads.

Two zones: 'postgres' (existing per-feed writers in modal/<source>/) and
'r2' (Cloudflare R2, parquet+zstd, single bucket dex-raw-landing-zone).

The metadata always lands in bulk_ingest.feed_ingest_runs regardless of
where the payload goes — the dashboard never has to ask "where did this
feed write today."

See plans/2026-05-07-fmcsa-front-of-house.md and the R2 landing addendum.
"""

from .base import LandingResult, LandingZone, R2_DEFAULT_BUCKET
from .ledger import (
    HeartbeatLoop,
    RunResult,
    classify_exception,
    compute_outcome,
    heartbeat,
    record_run,
)
from .r2 import R2Landing

__all__ = [
    "HeartbeatLoop",
    "LandingResult",
    "LandingZone",
    "R2Landing",
    "R2_DEFAULT_BUCKET",
    "RunResult",
    "classify_exception",
    "compute_outcome",
    "heartbeat",
    "record_run",
]
