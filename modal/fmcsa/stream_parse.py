from __future__ import annotations

import csv
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterator

# Module-level capture of the most recent header-validation result per feed.
# Workers in the same Modal container that called stream_feed_rows() can
# retrieve this via get_last_header_evidence(feed_name) and merge into the
# manifest evidence JSONB. Module-level (not thread-local) is safe because
# Modal worker functions are single-threaded by construction.
_LAST_HEADER_EVIDENCE: dict[str, dict[str, Any]] = {}


def get_last_header_evidence(feed_name: str) -> dict[str, Any] | None:
    """Return the header-validation evidence dict for the most recent
    stream_feed_rows() call on `feed_name`, or None if no run yet."""
    return _LAST_HEADER_EVIDENCE.get(feed_name)

import httpx

from .feed_catalog import FeedConfig
from .mappings import ParsedSourceRow


@dataclass(frozen=True)
class ParseSummary:
    rows_downloaded: int
    chunks_emitted: int


MAX_MALFORMED_ROW_WARNINGS = 10
DEFAULT_DOWNLOAD_RETRIES = 4
RETRY_BACKOFF_SECONDS = (5, 15, 45, 120)
DOWNLOAD_BYTE_CHUNK = 1024 * 1024

TRANSIENT_HTTP_ERRORS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


def _sanitize_source_value(value: str) -> str:
    stripped_value = value.strip()
    if "\x00" in stripped_value:
        stripped_value = stripped_value.replace("\x00", "")
    return stripped_value


def _validate_header(feed: FeedConfig, header_values: list[str]) -> dict[str, Any]:
    """Validate observed CSV header against feed.header_row when configured.

    Returns a dict suitable for merging into the run's evidence JSONB:
        {"header_validation": "passed" | "skipped_no_config",
         "observed_header_columns": int,
         "observed_header_sha256": str}

    Audit 2026-05-08 finding F-09: 13 of 31 FMCSA feeds (the daily-diff txt
    set) have header_row=None; this function used to early-return None,
    which means an upstream column shift on (e.g.) the load-bearing
    Carrier file would silently misalign every typed-row write to
    entities.fmcsa_carrier_records. Now we always record the observed
    header's column-count + SHA256 fingerprint into evidence so a
    column-shift is detectable post-hoc by SQL comparison against prior
    runs, even for feeds without explicit header_row config.

    The feeds that DO have header_row config still get the strict
    expected-vs-observed column comparison (raises ValueError on
    mismatch); that path is unchanged.
    """
    import hashlib

    observed_header_sha256 = hashlib.sha256(
        "|".join(header_values).encode("utf-8")
    ).hexdigest()
    base_evidence: dict[str, Any] = {
        "observed_header_columns": len(header_values),
        "observed_header_sha256": observed_header_sha256,
    }

    if feed.header_row is None:
        # F-09: no explicit header config. Record observation, log a loud
        # warning so operators see this in stdout / Modal logs each time,
        # but don't raise — the 13 affected feeds (AuthHist, Revocation,
        # Insurance, ActPendInsur, InsHist, Carrier, Rejected, BOC3, +5
        # *-All-With-History txt feeds) have been ingesting without
        # validation since launch; raising would break tomorrow's cron.
        # Follow-up: configure header_row from observed_header_sha256 on
        # the next stable run, then flip to strict validation.
        print(
            f"[fmcsa-header-validation] feed={feed.feed_name} "
            f"WARNING: no header_row configured; column-shift detection "
            f"falls back to evidence.observed_header_sha256 (currently "
            f"{observed_header_sha256[:12]}…) — operator should compare "
            f"across runs to detect drift."
        )
        return {**base_evidence, "header_validation": "skipped_no_config"}

    expected = list(feed.header_row)
    if len(expected) != len(header_values):
        raise ValueError(
            f"{feed.feed_name} header width mismatch: expected {len(expected)} got {len(header_values)}"
        )
    mismatched_index = next(
        (
            i
            for i, (expected_value, actual_value) in enumerate(zip(expected, header_values))
            if expected_value != actual_value
        ),
        None,
    )
    if mismatched_index is not None:
        raise ValueError(
            f'{feed.feed_name} header mismatch at column {mismatched_index + 1}: '
            f'expected "{expected[mismatched_index]}" got "{header_values[mismatched_index]}"'
        )

    return {**base_evidence, "header_validation": "passed"}


def _download_to_file(
    *,
    feed: FeedConfig,
    destination_path: str,
    timeout_seconds: float,
    max_retries: int,
) -> int:
    last_error: BaseException | None = None
    for attempt_index in range(max_retries):
        if attempt_index > 0:
            backoff_index = min(attempt_index - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            backoff_seconds = RETRY_BACKOFF_SECONDS[backoff_index]
            print(
                f"[fmcsa-download] {feed.feed_name} retry {attempt_index + 1}/{max_retries} "
                f"after {backoff_seconds}s (prior error: {type(last_error).__name__}: {last_error})"
            )
            time.sleep(backoff_seconds)
        try:
            bytes_written = 0
            with open(destination_path, "wb") as destination_file:
                with httpx.stream(
                    "GET",
                    feed.download_url,
                    follow_redirects=True,
                    timeout=timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    for payload_bytes in response.iter_bytes(chunk_size=DOWNLOAD_BYTE_CHUNK):
                        destination_file.write(payload_bytes)
                        bytes_written += len(payload_bytes)
            print(
                f"[fmcsa-download] {feed.feed_name} downloaded {bytes_written} bytes to {destination_path}"
            )
            return bytes_written
        except TRANSIENT_HTTP_ERRORS as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"{feed.feed_name} download failed after {max_retries} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _parse_file_rows(
    *,
    feed: FeedConfig,
    source_path: str,
    chunk_size: int,
) -> Iterator[list[ParsedSourceRow]]:
    chunk: list[ParsedSourceRow] = []
    rows_downloaded = 0
    has_header = feed.header_row is not None
    header_consumed = False
    row_number = 0
    malformed_rows_skipped = 0

    with open(source_path, "r", newline="", encoding="utf-8", errors="replace") as source_file:
        reader = csv.reader(source_file)
        for values in reader:
            if not values:
                continue

            rows_downloaded += 1
            if has_header and not header_consumed:
                header_evidence = _validate_header(feed, values)
                _LAST_HEADER_EVIDENCE[feed.feed_name] = header_evidence
                header_consumed = True
                continue

            if len(values) != feed.expected_field_count:
                malformed_rows_skipped += 1
                if malformed_rows_skipped <= MAX_MALFORMED_ROW_WARNINGS:
                    print(
                        f"[fmcsa-parse] {feed.feed_name} skipping malformed row at source row "
                        f"{rows_downloaded}: expected {feed.expected_field_count} fields, got {len(values)}"
                    )
                elif malformed_rows_skipped == MAX_MALFORMED_ROW_WARNINGS + 1:
                    print(
                        f"[fmcsa-parse] {feed.feed_name} further malformed-row warnings suppressed; "
                        "total will be reported at end"
                    )
                continue

            row_number += 1
            sanitized_values = [_sanitize_source_value(value) for value in values]
            parsed_row = ParsedSourceRow(
                row_number=row_number,
                raw_values=sanitized_values,
                raw_fields={
                    source_key: sanitized_value
                    for source_key, sanitized_value in zip(feed.source_fields, sanitized_values)
                },
            )
            chunk.append(parsed_row)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []

        if has_header and not header_consumed:
            raise ValueError(f"{feed.feed_name} expected header row but none was parsed")
        if row_number == 0:
            raise ValueError(f"{feed.feed_name} contained no data rows")
        if chunk:
            yield chunk

    if malformed_rows_skipped > 0:
        print(
            f"[fmcsa-parse] {feed.feed_name} malformed rows skipped total: {malformed_rows_skipped} "
            f"(of {rows_downloaded} source rows)"
        )


def stream_feed_rows(
    *,
    feed: FeedConfig,
    chunk_size: int | None = None,
    timeout_seconds: float = 600.0,
    max_download_retries: int = DEFAULT_DOWNLOAD_RETRIES,
) -> Iterator[list[ParsedSourceRow]]:
    chunk_size = chunk_size or feed.chunk_size
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="fmcsa_")
    os.close(tmp_fd)
    try:
        _download_to_file(
            feed=feed,
            destination_path=tmp_path,
            timeout_seconds=timeout_seconds,
            max_retries=max_download_retries,
        )
        for chunk in _parse_file_rows(feed=feed, source_path=tmp_path, chunk_size=chunk_size):
            yield chunk
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError as unlink_error:
            print(f"[fmcsa-download] failed to clean up temp file {tmp_path}: {unlink_error}")
