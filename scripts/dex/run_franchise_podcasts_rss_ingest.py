#!/usr/bin/env python3
"""Franchise-industry podcast episode metadata via RSS — JSONL to R2.

One-shot per-feed RSS fetch + parse + JSONL write + raw XML preservation.
No RW source DDL, no MV, no bridge. R2 is the deliverable surface.

For shows without a public RSS feed (Spotify-locked distribution etc.), an
Apple Podcasts AMP-API fallback path is used: the show's bearer token is
extracted from Apple's public JS bundle, then per-episode AMP calls populate
the same 22-field schema (audio_url, audio_length_bytes, audio_mime_type,
episode_duration_seconds are NULL — Apple gates those behind user auth).

Directive: ~/Desktop/hq/directives/2026-05-10-franchise-podcasts-rss-metadata-r2-ingest.md

Usage:
  doppler run --project hq-all --config prd --command \\
    'bash -c "cd apps/data-engine-x && \\
     uv run --with feedparser --with requests --with boto3 --with psycopg \\
     --with python-dateutil python3 scripts/run_franchise_podcasts_rss_ingest.py"'

  # Run only one show (re-run support):
  doppler run --project hq-all --config prd --command \\
    'bash -c "cd apps/data-engine-x && \\
     uv run --with feedparser --with requests --with boto3 --with psycopg \\
     --with python-dateutil python3 scripts/run_franchise_podcasts_rss_ingest.py \\
     --only-slug franchise-growth-show"'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import boto3
import feedparser
import psycopg
import requests
from dateutil import parser as dateparser
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_BASE = "podcasts"

USER_AGENT = "data-engine-x/podcast-rss-ingest tools@substrate.build"
HTTP_TIMEOUT_SECONDS = 30


@dataclass
class Show:
    slug: str
    feed_url: str | None  # None signals an unresolved §2.1 feed → skipped run row.


SHOWS: list[Show] = [
    Show("franchise-empires", "https://feeds.megaphone.fm/franchise-empires"),
    Show("franchise-masters", "https://feeds.podetize.com/rss/BZV_2qfY82"),
    Show("american-franchise-academy", "https://app.kajabi.com/podcasts/2147490809/feed"),
    Show("infinite-franchisee", "https://anchor.fm/s/5ce10b0c/podcast/rss"),
    Show("franchise-u", "https://feeds.blubrry.com/feeds/1474940.xml"),
    # Spotify-locked distribution; no public RSS. AMP fallback per docstring.
    Show("franchise-growth-show", "apple-amp://1814607922"),
]


APPLE_AMP_PREFIX = "apple-amp://"
APPLE_PODCASTS_BASE = "https://podcasts.apple.com"
APPLE_AMP_BASE = "https://amp-api.podcasts.apple.com/v1/catalog/us"
# JS-bundle URL pattern: /assets/index~<hash>.js — discovered at runtime.
APPLE_BUNDLE_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("podcast-rss-ingest")


log = _logger()


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        log.error("required env var %s is missing", name)
        sys.exit(2)
    return v


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _db_conn() -> psycopg.Connection:
    return psycopg.connect(_required_env("DEX_DB_URL_DIRECT"))


_ITEM_RE = re.compile(r"<item\b[^>]*>.*?</item\s*>", re.DOTALL | re.IGNORECASE)


def _parse_duration(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        m, sec = nums
        return m * 60 + sec
    if len(nums) == 3:
        h, m, sec = nums
        return h * 3600 + m * 60 + sec
    return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_pub_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        dt = dateparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _episode_description(entry: dict[str, Any]) -> str | None:
    content_list = entry.get("content")
    if isinstance(content_list, list) and content_list:
        v = content_list[0].get("value")
        if v:
            return v
    summary = entry.get("summary")
    return summary if summary else None


def _first_enclosure(entry: dict[str, Any]) -> dict[str, Any]:
    enclosures = entry.get("enclosures")
    if isinstance(enclosures, list) and enclosures:
        return enclosures[0]
    return {}


def _channel_image_url(feed_feed: dict[str, Any]) -> str | None:
    image = feed_feed.get("image")
    if isinstance(image, dict):
        href = image.get("href")
        if href:
            return href
    itunes_image = feed_feed.get("itunes_image")
    if isinstance(itunes_image, dict):
        return itunes_image.get("href")
    if isinstance(itunes_image, str) and itunes_image:
        return itunes_image
    return None


def _episode_image_url(entry: dict[str, Any]) -> str | None:
    image = entry.get("image")
    if isinstance(image, dict):
        href = image.get("href")
        if href:
            return href
    itunes_image = entry.get("itunes_image")
    if isinstance(itunes_image, dict):
        return itunes_image.get("href")
    if isinstance(itunes_image, str) and itunes_image:
        return itunes_image
    return None


def _build_episode_record(
    *,
    show_slug: str,
    show_feed_url: str,
    feed_feed: dict[str, Any],
    entry: dict[str, Any],
    raw_item_xml: str | None,
    ingested_at_iso: str,
) -> dict[str, Any]:
    enclosure = _first_enclosure(entry)
    return {
        "show_name": feed_feed.get("title") or "",
        "show_slug": show_slug,
        "show_feed_url": show_feed_url,
        "show_link": feed_feed.get("link") or None,
        "show_image_url": _channel_image_url(feed_feed),
        "episode_guid": entry.get("id") or "",
        "episode_title": entry.get("title") or "",
        "episode_link": entry.get("link") or None,
        "episode_description": _episode_description(entry),
        "episode_pub_date": _parse_pub_date(entry.get("published")),
        "episode_duration_seconds": _parse_duration(entry.get("itunes_duration")),
        "episode_number": _parse_int(entry.get("itunes_episode")),
        "season_number": _parse_int(entry.get("itunes_season")),
        "episode_type": entry.get("itunes_episodetype") or None,
        "audio_url": enclosure.get("href") or None,
        "audio_length_bytes": _parse_int(enclosure.get("length")),
        "audio_mime_type": enclosure.get("type") or None,
        "episode_image_url": _episode_image_url(entry),
        "explicit": entry.get("itunes_explicit") or None,
        "author": entry.get("itunes_author") or None,
        "raw_item_xml": raw_item_xml,
        "ingested_at": ingested_at_iso,
    }


def _insert_run(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    show_slug: str,
    show_feed_url: str | None,
) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.podcast_rss_ingest_runs
                (id, snapshot_date, show_slug, show_feed_url, status, started_at)
            VALUES (%s, %s, %s, %s, 'running', now())
            """,
            (run_id, snapshot_date, show_slug, show_feed_url),
        )
    conn.commit()
    return run_id


def _finalize_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    http_status: int | None = None,
    raw_xml_bytes: int | None = None,
    episodes_count: int | None = None,
    jsonl_bytes: int | None = None,
    oldest_pub_date: str | None = None,
    newest_pub_date: str | None = None,
    r2_episodes_key: str | None = None,
    r2_raw_xml_key: str | None = None,
    show_name_actual: str | None = None,
    error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.podcast_rss_ingest_runs
               SET status = %s,
                   http_status = %s,
                   raw_xml_bytes = %s,
                   episodes_count = %s,
                   jsonl_bytes = %s,
                   oldest_pub_date = %s,
                   newest_pub_date = %s,
                   r2_episodes_key = %s,
                   r2_raw_xml_key = %s,
                   show_name_actual = %s,
                   error_message = %s,
                   notes = %s,
                   finished_at = now(),
                   duration_seconds = EXTRACT(EPOCH FROM (now() - started_at))
             WHERE id = %s
            """,
            (
                status,
                http_status,
                raw_xml_bytes,
                episodes_count,
                jsonl_bytes,
                oldest_pub_date,
                newest_pub_date,
                r2_episodes_key,
                r2_raw_xml_key,
                show_name_actual,
                error_message,
                Jsonb(notes) if notes is not None else None,
                run_id,
            ),
        )
    conn.commit()


def _extract_apple_bearer_token() -> str:
    """Scrape Apple Podcasts JS bundle for the public bearer JWT.

    The token is served to every web visitor — not user-bound — and rotates
    every ~6 months. Re-extract on each run.
    """
    home_html = requests.get(
        f"{APPLE_PODCASTS_BASE}/us",
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    ).text
    bundles = re.findall(r'src="(/assets/index~[a-f0-9]+\.js)"', home_html)
    if not bundles:
        raise RuntimeError("could not locate Apple JS bundle in podcasts.apple.com/us")
    bundle_url = APPLE_PODCASTS_BASE + bundles[0]
    bundle_text = requests.get(
        bundle_url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    ).text
    m = APPLE_BUNDLE_TOKEN_RE.search(bundle_text)
    if not m:
        raise RuntimeError(f"could not extract bearer JWT from {bundle_url}")
    return m.group(0)


def _apple_amp_get(url: str, token: str) -> dict[str, Any]:
    r = requests.get(
        url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": APPLE_PODCASTS_BASE,
            "Referer": f"{APPLE_PODCASTS_BASE}/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    r.raise_for_status()
    return r.json()


def _apple_artwork_url(artwork: dict[str, Any] | None) -> str | None:
    if not artwork:
        return None
    url_tmpl = artwork.get("url")
    if not url_tmpl:
        return None
    w = artwork.get("width") or 600
    h = artwork.get("height") or 600
    return (
        url_tmpl.replace("{w}", str(w))
        .replace("{h}", str(h))
        .replace("{f}", "jpg")
        .replace("{c}", "bb")
    )


def _process_show_via_apple_amp(
    show: Show,
    *,
    snapshot_date: date,
    s3,
    conn: psycopg.Connection,
) -> dict[str, Any]:
    apple_id = show.feed_url.removeprefix(APPLE_AMP_PREFIX)
    run_id = _insert_run(
        conn,
        snapshot_date=snapshot_date,
        show_slug=show.slug,
        show_feed_url=show.feed_url,
    )

    try:
        token = _extract_apple_bearer_token()
    except Exception as exc:
        _finalize_run(conn, run_id, status="failed", error_message=f"apple_token_extract: {exc}")
        log.error("%s: token extract error: %s", show.slug, exc)
        return {"slug": show.slug, "status": "failed", "error": str(exc)}

    try:
        show_resp = _apple_amp_get(
            f"{APPLE_AMP_BASE}/podcasts/{apple_id}"
            f"?include=episodes&limit%5Bepisodes%5D=200",
            token,
        )
    except Exception as exc:
        _finalize_run(conn, run_id, status="failed", error_message=f"apple_show_fetch: {exc}")
        log.error("%s: show fetch error: %s", show.slug, exc)
        return {"slug": show.slug, "status": "failed", "error": str(exc)}

    pod = show_resp["data"][0]
    pod_attrs = pod["attributes"]
    ep_ids = [e["id"] for e in pod["relationships"]["episodes"]["data"]]
    log.info("%s: %d episode IDs from Apple AMP", show.slug, len(ep_ids))

    show_name = pod_attrs.get("name") or ""
    show_link = pod_attrs.get("url") or None
    show_image_url = _apple_artwork_url(pod_attrs.get("artwork"))
    author = pod_attrs.get("artistName") or None

    raw_payloads: dict[str, Any] = {"show": show_resp, "episodes": []}
    ingested_at_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []

    for idx, eid in enumerate(ep_ids):
        try:
            ep_resp = _apple_amp_get(f"{APPLE_AMP_BASE}/podcast-episodes/{eid}", token)
        except Exception as exc:
            log.warning("%s: episode %s fetch failed: %s — skipping", show.slug, eid, exc)
            continue
        raw_payloads["episodes"].append(ep_resp)
        a = ep_resp["data"][0]["attributes"]
        dur_ms = a.get("durationInMilliseconds")
        rec = {
            "show_name": show_name,
            "show_slug": show.slug,
            "show_feed_url": show.feed_url,
            "show_link": show_link,
            "show_image_url": show_image_url,
            "episode_guid": a.get("guid") or eid,
            "episode_title": a.get("name") or "",
            "episode_link": a.get("url") or None,
            "episode_description": (a.get("description") or {}).get("standard") or None,
            "episode_pub_date": _parse_pub_date(a.get("releaseDateTime")),
            "episode_duration_seconds": int(dur_ms / 1000) if dur_ms else None,
            "episode_number": _parse_int(a.get("episodeNumber")),
            "season_number": _parse_int(a.get("seasonNumber")),
            "episode_type": a.get("kind") or None,
            "audio_url": a.get("assetUrl") or None,
            "audio_length_bytes": None,
            "audio_mime_type": None,
            "episode_image_url": _apple_artwork_url(a.get("artwork")),
            "explicit": a.get("contentAdvisory") or None,
            "author": a.get("artistName") or author,
            "raw_item_xml": None,
            "ingested_at": ingested_at_iso,
        }
        records.append(rec)
        if idx and idx % 10 == 0:
            log.info("  %s: %d/%d episodes fetched", show.slug, idx, len(ep_ids))
        time.sleep(0.1)

    if not records:
        _finalize_run(
            conn,
            run_id,
            status="failed",
            show_name_actual=show_name,
            error_message="no_episodes_fetched",
        )
        return {"slug": show.slug, "status": "failed", "error": "no_episodes_fetched"}

    # Raw JSON preservation (Apple AMP analog of feed-raw.xml).
    raw_json_body = json.dumps(raw_payloads, ensure_ascii=False, default=str).encode("utf-8")
    raw_json_bytes = len(raw_json_body)
    r2_raw_key = f"{R2_PREFIX_BASE}/{show.slug}/feed-raw.apple-amp.json"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=r2_raw_key,
        Body=raw_json_body,
        ContentType="application/json",
    )
    log.info("%s: uploaded raw AMP JSON (%d bytes) to %s", show.slug, raw_json_bytes, r2_raw_key)

    jsonl_body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records) + "\n"
    jsonl_bytes = len(jsonl_body.encode("utf-8"))
    r2_episodes_key = f"{R2_PREFIX_BASE}/{show.slug}/episodes.jsonl"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=r2_episodes_key,
        Body=jsonl_body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )
    log.info(
        "%s: uploaded %d episodes (%d bytes) to %s",
        show.slug,
        len(records),
        jsonl_bytes,
        r2_episodes_key,
    )

    pub_dates = sorted([r["episode_pub_date"] for r in records if r["episode_pub_date"]])
    oldest = pub_dates[0] if pub_dates else None
    newest = pub_dates[-1] if pub_dates else None

    _finalize_run(
        conn,
        run_id,
        status="completed",
        http_status=200,
        raw_xml_bytes=raw_json_bytes,
        episodes_count=len(records),
        jsonl_bytes=jsonl_bytes,
        oldest_pub_date=oldest,
        newest_pub_date=newest,
        r2_episodes_key=r2_episodes_key,
        r2_raw_xml_key=r2_raw_key,
        show_name_actual=show_name,
        notes={"source": "apple-amp", "apple_id": apple_id, "episode_ids_requested": len(ep_ids), "episodes_fetched": len(records)},
    )
    return {
        "slug": show.slug,
        "status": "completed",
        "episodes": len(records),
        "jsonl_bytes": jsonl_bytes,
        "raw_xml_bytes": raw_json_bytes,
        "source": "apple-amp",
    }


def _process_show(
    show: Show,
    *,
    snapshot_date: date,
    s3,
    conn: psycopg.Connection,
) -> dict[str, Any]:
    log.info("=== %s ===", show.slug)

    if show.feed_url and show.feed_url.startswith(APPLE_AMP_PREFIX):
        return _process_show_via_apple_amp(
            show, snapshot_date=snapshot_date, s3=s3, conn=conn
        )

    if show.feed_url is None:
        run_id = _insert_run(
            conn,
            snapshot_date=snapshot_date,
            show_slug=show.slug,
            show_feed_url=None,
        )
        _finalize_run(
            conn,
            run_id,
            status="skipped",
            error_message="RSS not resolvable via 4-step protocol",
        )
        log.warning("%s: skipped (unresolved feed)", show.slug)
        return {"slug": show.slug, "status": "skipped"}

    run_id = _insert_run(
        conn,
        snapshot_date=snapshot_date,
        show_slug=show.slug,
        show_feed_url=show.feed_url,
    )

    try:
        resp = requests.get(
            show.feed_url,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
    except Exception as exc:
        _finalize_run(conn, run_id, status="failed", error_message=f"fetch_error: {exc}")
        log.error("%s: fetch error: %s", show.slug, exc)
        return {"slug": show.slug, "status": "failed", "error": str(exc)}

    if resp.status_code != 200:
        _finalize_run(
            conn,
            run_id,
            status="failed",
            http_status=resp.status_code,
            error_message=f"http_{resp.status_code}",
        )
        log.error("%s: HTTP %s", show.slug, resp.status_code)
        return {"slug": show.slug, "status": "failed", "http_status": resp.status_code}

    raw_text = resp.text
    raw_xml_bytes = len(resp.content)

    # Upload raw XML verbatim. ContentType only — no ContentEncoding (L42).
    r2_raw_key = f"{R2_PREFIX_BASE}/{show.slug}/feed-raw.xml"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=r2_raw_key,
        Body=resp.content,
        ContentType="application/xml",
    )
    log.info("%s: uploaded raw XML (%d bytes) to %s", show.slug, raw_xml_bytes, r2_raw_key)

    parsed = feedparser.parse(raw_text)
    feed_feed: dict[str, Any] = parsed.feed if parsed.feed else {}
    entries: list[dict[str, Any]] = parsed.entries or []

    raw_items = _ITEM_RE.findall(raw_text)
    item_count_divergence = len(raw_items) != len(entries)

    ingested_at_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        raw_item_xml = raw_items[idx] if idx < len(raw_items) and not item_count_divergence else None
        records.append(
            _build_episode_record(
                show_slug=show.slug,
                show_feed_url=show.feed_url,
                feed_feed=feed_feed,
                entry=entry,
                raw_item_xml=raw_item_xml,
                ingested_at_iso=ingested_at_iso,
            )
        )

    if not records:
        _finalize_run(
            conn,
            run_id,
            status="failed",
            http_status=200,
            raw_xml_bytes=raw_xml_bytes,
            r2_raw_xml_key=r2_raw_key,
            show_name_actual=feed_feed.get("title"),
            error_message="no_entries",
        )
        log.error("%s: parsed feed had zero entries", show.slug)
        return {"slug": show.slug, "status": "failed", "error": "no_entries"}

    jsonl_body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records) + "\n"
    jsonl_bytes = len(jsonl_body.encode("utf-8"))

    r2_episodes_key = f"{R2_PREFIX_BASE}/{show.slug}/episodes.jsonl"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=r2_episodes_key,
        Body=jsonl_body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )
    log.info(
        "%s: uploaded %d episodes (%d bytes) to %s",
        show.slug,
        len(records),
        jsonl_bytes,
        r2_episodes_key,
    )

    pub_dates_iso = [r["episode_pub_date"] for r in records if r["episode_pub_date"]]
    pub_dates_iso.sort()
    oldest = pub_dates_iso[0] if pub_dates_iso else None
    newest = pub_dates_iso[-1] if pub_dates_iso else None

    notes: dict[str, Any] = {
        "raw_item_count": len(raw_items),
        "feedparser_entry_count": len(entries),
        "item_count_divergence": item_count_divergence,
    }

    _finalize_run(
        conn,
        run_id,
        status="completed",
        http_status=200,
        raw_xml_bytes=raw_xml_bytes,
        episodes_count=len(records),
        jsonl_bytes=jsonl_bytes,
        oldest_pub_date=oldest,
        newest_pub_date=newest,
        r2_episodes_key=r2_episodes_key,
        r2_raw_xml_key=r2_raw_key,
        show_name_actual=feed_feed.get("title"),
        notes=notes,
    )
    return {
        "slug": show.slug,
        "status": "completed",
        "episodes": len(records),
        "jsonl_bytes": jsonl_bytes,
        "raw_xml_bytes": raw_xml_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only-slug",
        action="append",
        default=[],
        help="Process only the named show slug. Repeatable. Default: all shows.",
    )
    parser.add_argument(
        "--min-completed",
        type=int,
        default=4,
        help="Floor on completed-show count for run-level success (default 4).",
    )
    args = parser.parse_args()

    if args.only_slug:
        targets = [s for s in SHOWS if s.slug in set(args.only_slug)]
        unknown = set(args.only_slug) - {s.slug for s in SHOWS}
        if unknown:
            log.error("unknown slug(s): %s", sorted(unknown))
            return 2
    else:
        targets = SHOWS

    snapshot_date = datetime.now(timezone.utc).date()
    log.info("snapshot_date=%s shows=%d", snapshot_date, len(targets))

    s3 = _s3_client()
    results: list[dict[str, Any]] = []

    with _db_conn() as conn:
        # Clear any prior runs for these shows on this snapshot_date so the
        # re-run is the single row of record.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ops.podcast_rss_ingest_runs "
                "WHERE snapshot_date = %s AND show_slug = ANY(%s)",
                (snapshot_date, [s.slug for s in targets]),
            )
        conn.commit()

        for show in targets:
            results.append(_process_show(show, snapshot_date=snapshot_date, s3=s3, conn=conn))

    log.info("=== summary ===")
    for r in results:
        log.info("  %s", r)

    completed = sum(1 for r in results if r["status"] == "completed")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    log.info("completed=%d failed=%d skipped=%d", completed, failed, skipped)

    if completed < args.min_completed:
        log.error("completed=%d below floor of %d — exiting non-zero", completed, args.min_completed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
