"""Per-feed FMCSA upstream-freshness probes.

Replaces the fixed-cron dispatch model with a per-tick availability check
of each feed's upstream URL. Two strategies cover all 31 feeds in the
catalog (verified empirically against data.transportation.gov 2026-04-30):

  * head_redirect_filename — used for /download/{4x4}/text%2Fplain URLs
    (feed_group='txt'). HEAD returns a 302 whose Location header carries
    the published filename. Daily-diff feeds embed a date in the filename
    (authhist_2026_04_30.txt) — that date is the published feed_date.
    "All With History" snapshots use a stable filename, so the per-version
    file UUID embedded in the redirect path is the freshness fingerprint.

  * get_headers_last_modified — used for /api/views/{4x4}/rows.csv URLs
    (feed_group='sms','csv'). Socrata's view endpoint rejects HEAD with
    404, but a GET exposes a usable Last-Modified header. We open the
    response with httpx.stream(), read just the headers, and close
    without consuming the body.

  * disabled — escape hatch for any feed that needs a future fallback
    (Socrata 4x4 metadata API, listing scrape) without removing the row.
    Probe always returns is_fresh=False with probe_strategy='disabled'.

The probe is fail-safe by default: any HTTP/parse error short-circuits
to is_fresh=False with probe_error populated. Rationale — fail-open
would fire a real ingest on every probe failure, wasting orchestrator
capacity. last_probe_at is stamped regardless of outcome so the audit
trail captures "we checked at HH:MM and the feed wasn't ready yet."

Fan-in to dispatch: the heartbeat compares the probe result against the
schedule_config row's last_successful_feed_date / last_probe_signal to
decide is_fresh. The probe itself does not read the manifest — it only
returns what upstream told us.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .feed_catalog import FeedConfig

PROBE_HTTP_TIMEOUT_SECONDS = 30.0

STRATEGY_HEAD_REDIRECT_FILENAME = "head_redirect_filename"
STRATEGY_GET_HEADERS_LAST_MODIFIED = "get_headers_last_modified"
STRATEGY_DISABLED = "disabled"

ALL_STRATEGIES: frozenset[str] = frozenset(
    {
        STRATEGY_HEAD_REDIRECT_FILENAME,
        STRATEGY_GET_HEADERS_LAST_MODIFIED,
        STRATEGY_DISABLED,
    }
)

# Daily-diff snapshot filenames look like: authhist_2026_04_30.txt,
# carrier_2026_04_30.txt. The "All With History" snapshots have no
# date — e.g. authhist_allwithhistory.txt — so this only matches when
# a date is present.
_FILENAME_DATE_PATTERN = re.compile(r"_(\d{4})_(\d{2})_(\d{2})\.[A-Za-z0-9]+$")

# Socrata redirect path: /api/views/{4x4}/files/{file_uuid}?filename=...
# The file_uuid changes whenever a new version is published, so it is a
# stable freshness fingerprint for snapshots that lack a date in the
# filename.
_FILE_UUID_PATTERN = re.compile(r"/files/([0-9a-fA-F-]{8,})")


@dataclass(frozen=True)
class ProbeResult:
    """What a freshness probe observed about an upstream feed.

    `is_fresh` answers a SUFFICIENT-not-NECESSARY question: "did the
    probe see something newer than `last_known_signal`?" The heartbeat
    layer separately gates on per-day idempotency
    (`last_successful_feed_date`) and earliest-check-time, so a
    `is_fresh=True` result does not by itself trigger dispatch.

    `observed_signal` is opaque per-strategy — for filename-date probes
    it's the date string, for snapshot probes it's the file UUID, for
    Last-Modified probes it's the RFC1123 string. Comparing equality to
    the previously-stored signal is the only intended use.
    """

    is_fresh: bool
    probe_strategy: str
    observed_modified_at: datetime | None = None
    observed_signal: str | None = None
    observed_filename: str | None = None
    observed_feed_date: date | None = None
    probe_error: str | None = None
    probe_http_status: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_filename_date(filename: str) -> date | None:
    match = _FILENAME_DATE_PATTERN.search(filename)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_file_uuid(location_url: str) -> str | None:
    match = _FILE_UUID_PATTERN.search(location_url)
    return match.group(1) if match else None


def _parse_filename_from_location(location_url: str) -> str | None:
    """Pull the `filename=` query param from a Socrata redirect Location."""
    parsed = urlparse(location_url)
    if not parsed.query:
        # Some redirects encode the filename in the path tail.
        tail = parsed.path.rsplit("/", 1)[-1]
        return tail or None
    for part in parsed.query.split("&"):
        if part.startswith("filename="):
            return unquote(part[len("filename=") :]) or None
    return None


def _parse_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _probe_head_redirect_filename(
    *,
    feed: FeedConfig,
    last_known_signal: str | None,
    client: httpx.Client,
) -> ProbeResult:
    """HEAD with follow_redirects=False; parse Location for filename + date.

    The "fresh" decision:
      * If filename has a date and that date > the date encoded in
        last_known_signal → fresh.
      * If filename has no date (snapshot) but file_uuid changed →
        fresh.
      * Otherwise stale.
    """
    response = client.head(
        feed.download_url,
        follow_redirects=False,
        timeout=PROBE_HTTP_TIMEOUT_SECONDS,
    )
    status_code = response.status_code

    # Socrata's /download endpoint serves a 302; if it changes to 200
    # (direct file) we can still use the response headers.
    location = response.headers.get("location")
    if status_code in (301, 302, 303, 307, 308) and not location:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=STRATEGY_HEAD_REDIRECT_FILENAME,
            probe_http_status=status_code,
            probe_error="redirect with no Location header",
        )

    filename: str | None = None
    file_uuid: str | None = None
    feed_date: date | None = None

    if location:
        filename = _parse_filename_from_location(location)
        file_uuid = _parse_file_uuid(location)
        if filename:
            feed_date = _parse_filename_date(filename)
    else:
        # 200 OK direct: try Content-Disposition.
        cd = response.headers.get("content-disposition", "")
        match = re.search(r'filename="?([^";]+)"?', cd)
        if match:
            filename = match.group(1)
            feed_date = _parse_filename_date(filename)

    if not filename and not file_uuid:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=STRATEGY_HEAD_REDIRECT_FILENAME,
            probe_http_status=status_code,
            probe_error="could not extract filename or file_uuid from redirect",
        )

    if feed_date is not None:
        signal = f"date:{feed_date.isoformat()}"
        observed_modified_at = datetime(
            feed_date.year, feed_date.month, feed_date.day, tzinfo=timezone.utc
        )
    elif file_uuid:
        signal = f"uuid:{file_uuid}"
        observed_modified_at = None
    else:
        signal = f"name:{filename}"
        observed_modified_at = None

    is_fresh = signal != last_known_signal

    return ProbeResult(
        is_fresh=is_fresh,
        probe_strategy=STRATEGY_HEAD_REDIRECT_FILENAME,
        observed_modified_at=observed_modified_at,
        observed_signal=signal,
        observed_filename=filename,
        observed_feed_date=feed_date,
        probe_http_status=status_code,
    )


def _probe_get_headers_last_modified(
    *,
    feed: FeedConfig,
    last_known_signal: str | None,
    client: httpx.Client,
) -> ProbeResult:
    """Open a streaming GET, read headers, close without consuming body.

    Required because Socrata's /api/views/{4x4}/rows.csv?accessType=DOWNLOAD
    rejects HEAD with 404.
    """
    with client.stream(
        "GET",
        feed.download_url,
        follow_redirects=True,
        timeout=PROBE_HTTP_TIMEOUT_SECONDS,
    ) as response:
        status_code = response.status_code
        last_modified_header = (
            response.headers.get("last-modified")
            or response.headers.get("x-soda2-truth-last-modified")
        )

    if status_code != 200:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=STRATEGY_GET_HEADERS_LAST_MODIFIED,
            probe_http_status=status_code,
            probe_error=f"non-200 from upstream: {status_code}",
        )

    if not last_modified_header:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=STRATEGY_GET_HEADERS_LAST_MODIFIED,
            probe_http_status=status_code,
            probe_error="missing Last-Modified header",
        )

    observed_modified_at = _parse_http_date(last_modified_header)
    signal = f"lm:{last_modified_header}"
    is_fresh = signal != last_known_signal

    return ProbeResult(
        is_fresh=is_fresh,
        probe_strategy=STRATEGY_GET_HEADERS_LAST_MODIFIED,
        observed_modified_at=observed_modified_at,
        observed_signal=signal,
        probe_http_status=status_code,
    )


def probe_feed_freshness(
    *,
    feed: FeedConfig,
    strategy: str,
    last_known_signal: str | None = None,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Top-level probe entry point.

    Selects the strategy implementation, runs it against the feed's
    download_url, and translates any exception into a ProbeResult with
    probe_error populated. The probe NEVER raises — fail-safe semantics
    let the heartbeat treat probe errors as "do not dispatch this tick"
    without try/except cluttering the dispatch loop.
    """
    if strategy not in ALL_STRATEGIES:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=strategy,
            probe_error=f"unknown strategy: {strategy}",
        )

    if strategy == STRATEGY_DISABLED:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=STRATEGY_DISABLED,
        )

    owns_client = client is None
    http_client = client or httpx.Client(timeout=PROBE_HTTP_TIMEOUT_SECONDS)
    try:
        if strategy == STRATEGY_HEAD_REDIRECT_FILENAME:
            return _probe_head_redirect_filename(
                feed=feed,
                last_known_signal=last_known_signal,
                client=http_client,
            )
        return _probe_get_headers_last_modified(
            feed=feed,
            last_known_signal=last_known_signal,
            client=http_client,
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=strategy,
            probe_error=f"http error: {exc.__class__.__name__}: {exc}"[:500],
        )
    except Exception as exc:
        return ProbeResult(
            is_fresh=False,
            probe_strategy=strategy,
            probe_error=f"probe failure: {exc.__class__.__name__}: {exc}"[:500],
        )
    finally:
        if owns_client:
            http_client.close()
