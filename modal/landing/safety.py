"""Landing-side intent guards: separating 'we landed real data' from
'upstream genuinely had no data and we proved it.'

Caller contract: any ingest that can legitimately encounter zero rows must
explicitly assert which branch it took, with proof when the branch is
'upstream was empty.'

Example (USAspending API daily delta)::

    rows_loaded, upstream_count = run_ingest(...)
    assert_landed_or_explicit_empty(
        rows_loaded=rows_loaded,
        upstream_was_empty=(upstream_count == 0),
        upstream_proof={
            "source": "usaspending-api-v2-search-spending-by-award",
            "endpoint": "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            "request_params_hash": "...",
            "api_total_count": upstream_count,
            "checked_at": "2026-05-12T06:13:42Z",
            "feed_date": "2026-05-12",
        },
    )

Cycle: usaspending-poison-file-class-fix-v1 (2026-05-13)
"""

from __future__ import annotations

from typing import TypedDict


class UpstreamEmptyProof(TypedDict, total=False):
    source: str               # required — short label (e.g., "usaspending-api-v2")
    endpoint: str             # required — fully qualified URL the caller hit
    request_params_hash: str  # required — sha256 over normalized params
    api_total_count: int      # required when upstream returns a count field
    checked_at: str           # required — ISO8601 UTC timestamp of the probe
    feed_date: str            # required for daily feeds — ISO date the cron is for
    note: str                 # optional — free-form ("API returned 0 awards for date X")


REQUIRED_KEYS = {"source", "endpoint", "request_params_hash", "checked_at"}


def assert_landed_or_explicit_empty(
    *,
    rows_loaded: int,
    upstream_was_empty: bool,
    upstream_proof: UpstreamEmptyProof,
) -> None:
    """Raise unless EITHER rows_loaded > 0 OR upstream_was_empty=True with proof.

    Forbidden state: rows_loaded == 0 AND upstream_was_empty == False
        This is the poison-file class of bug: caller thought it ingested
        data, but landed zero rows. The writer (post-s1 fix) refuses to
        upload a 0-byte file; this assertion forces the caller to
        acknowledge the situation explicitly rather than silently
        continuing.

    Forbidden state: rows_loaded == 0 AND upstream_was_empty == True
                     AND upstream_proof missing required keys
        Caller MUST positively prove upstream was empty. A default {}
        is not proof. Required keys: source, endpoint, request_params_hash,
        checked_at (api_total_count required when applicable).
    """
    if rows_loaded > 0:
        return
    if not upstream_was_empty:
        raise RuntimeError(
            f"assert_landed_or_explicit_empty: rows_loaded={rows_loaded} but "
            f"upstream_was_empty=False — caller landed zero rows without "
            f"proving upstream was empty. This is the poison-file class of "
            f"bug. Pass upstream_was_empty=True with upstream_proof to "
            f"acknowledge an explicit-empty path."
        )
    missing = REQUIRED_KEYS - set(upstream_proof.keys())
    if missing:
        raise RuntimeError(
            f"assert_landed_or_explicit_empty: upstream_was_empty=True but "
            f"upstream_proof missing required keys: {sorted(missing)}. "
            f"Pass a dict with at minimum: source, endpoint, "
            f"request_params_hash, checked_at."
        )
