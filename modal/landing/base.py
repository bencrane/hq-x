"""Landing-zone protocol + result type.

Workers call write_batch(...) and get back a LandingResult that maps 1:1 to
bulk_ingest.feed_ingest_runs columns. Postgres workers can keep their
existing per-source writers; R2 workers go through R2Landing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from uuid import UUID

R2_DEFAULT_BUCKET = "dex-raw-landing-zone"


@dataclass(frozen=True)
class LandingResult:
    """Metadata for a single landing-zone write. Maps to ledger columns."""

    landing_zone: str  # 'postgres' | 'r2'
    rows_loaded: int
    payload_bytes: int  # bytes written to landing zone (R2 object size, or COPY bytes)

    # R2-only
    r2_bucket: str | None = None
    r2_object_key: str | None = None
    payload_format: str | None = None  # 'parquet_zstd' | 'jsonl_gz' | 'csv_gz'

    def to_ledger_columns(self) -> dict[str, Any]:
        """Shape the result into kwargs for mark_completed.

        rows_loaded is intentionally omitted — the orchestrator passes that
        explicitly to mark_completed because it has its own row-counting
        semantics (rows_downloaded vs rows_written), and double-passing
        causes a 'got multiple values' TypeError when the caller spreads
        this dict alongside an explicit rows_loaded kwarg.
        """
        return {
            "landing_zone": self.landing_zone,
            "payload_bytes": self.payload_bytes,
            "r2_bucket": self.r2_bucket,
            "r2_object_key": self.r2_object_key,
            "payload_format": self.payload_format,
        }


class LandingZone(Protocol):
    """Where the worker writes payload rows."""

    landing_zone: str

    def write_batch(
        self,
        *,
        source_id: str,
        feed_name: str,
        feed_date: date,
        run_id: UUID,
        rows: Iterable[dict[str, Any]],
    ) -> LandingResult: ...
