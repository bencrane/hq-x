"""
Measure FMCSA feed file sizes and row counts without ingesting data.
Uses HEAD requests + Socrata metadata API.
Outputs JSON sorted by file_size_bytes descending.
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "fmcsa-feed-measurement/1.0"

FEEDS = [
    {"feed_name": "AuthHist",                        "download_url": "https://data.transportation.gov/download/sn3k-dnx7/text%2Fplain"},
    {"feed_name": "Revocation",                      "download_url": "https://data.transportation.gov/download/pivg-szje/text%2Fplain"},
    {"feed_name": "Insurance",                       "download_url": "https://data.transportation.gov/download/mzmm-6xep/text%2Fplain"},
    {"feed_name": "ActPendInsur",                    "download_url": "https://data.transportation.gov/download/chgs-tx6x/text%2Fplain"},
    {"feed_name": "InsHist",                         "download_url": "https://data.transportation.gov/download/xkmg-ff2t/text%2Fplain"},
    {"feed_name": "Carrier",                         "download_url": "https://data.transportation.gov/download/6qg9-x4f8/text%2Fplain"},
    {"feed_name": "Rejected",                        "download_url": "https://data.transportation.gov/download/t3zq-c6n3/text%2Fplain"},
    {"feed_name": "BOC3",                            "download_url": "https://data.transportation.gov/download/fb8g-ngam/text%2Fplain"},
    {"feed_name": "InsHist - All With History",      "download_url": "https://data.transportation.gov/download/nzpz-e5xn/text%2Fplain"},
    {"feed_name": "BOC3 - All With History",         "download_url": "https://data.transportation.gov/download/gmxu-awv7/text%2Fplain"},
    {"feed_name": "ActPendInsur - All With History", "download_url": "https://data.transportation.gov/download/y77m-3nfx/text%2Fplain"},
    {"feed_name": "Rejected - All With History",     "download_url": "https://data.transportation.gov/download/9m5y-imtw/text%2Fplain"},
    {"feed_name": "AuthHist - All With History",     "download_url": "https://data.transportation.gov/download/wahn-z3rq/text%2Fplain"},
    {"feed_name": "Crash File",                      "download_url": "https://data.transportation.gov/api/views/aayw-vxb3/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Carrier - All With History",      "download_url": "https://data.transportation.gov/api/views/6eyk-hxee/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Inspections Per Unit",            "download_url": "https://data.transportation.gov/api/views/wt8s-2hbx/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Special Studies",                 "download_url": "https://data.transportation.gov/api/views/5qik-smay/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Revocation - All With History",   "download_url": "https://data.transportation.gov/api/views/sa6p-acbp/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Insur - All With History",        "download_url": "https://data.transportation.gov/api/views/ypjt-5ydn/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "OUT OF SERVICE ORDERS",           "download_url": "https://data.transportation.gov/api/views/p2mt-9ige/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Inspections and Citations",       "download_url": "https://data.transportation.gov/api/views/qbt8-7vic/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Vehicle Inspections and Violations", "download_url": "https://data.transportation.gov/api/views/876r-jsdb/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Company Census File",             "download_url": "https://data.transportation.gov/api/views/az4n-8mr2/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "Vehicle Inspection File",         "download_url": "https://data.transportation.gov/api/views/fx4q-ay7w/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS AB PassProperty",             "download_url": "https://data.transportation.gov/api/views/4y6x-dmck/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS C PassProperty",              "download_url": "https://data.transportation.gov/api/views/h9zy-gjn8/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS Input - Violation",           "download_url": "https://data.transportation.gov/api/views/8mt8-2mdr/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS Input - Inspection",          "download_url": "https://data.transportation.gov/api/views/rbkj-cgst/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS Input - Motor Carrier Census","download_url": "https://data.transportation.gov/api/views/kjg3-diqy/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS AB Pass",                     "download_url": "https://data.transportation.gov/api/views/m3ry-qcip/rows.csv?accessType=DOWNLOAD"},
    {"feed_name": "SMS C Pass",                      "download_url": "https://data.transportation.gov/api/views/h3zn-uid9/rows.csv?accessType=DOWNLOAD"},
]


def extract_dataset_id(url: str) -> str | None:
    # /download/<id>/text%2Fplain
    m = re.search(r"/download/([a-z0-9-]{9})/", url)
    if m:
        return m.group(1)
    # /api/views/<id>/rows.csv
    m = re.search(r"/api/views/([a-z0-9-]{9})/rows\.csv", url)
    if m:
        return m.group(1)
    return None


def get_row_count_from_metadata(dataset_id: str) -> tuple[int | None, str | None]:
    """
    Returns (row_count, source_label).
    Tries: Socrata metadata cachedContents → $select=count(*) → rows.csv count header.
    """
    # 1. Metadata cachedContents
    meta_url = f"https://data.transportation.gov/api/views/{dataset_id}.json"
    try:
        r = SESSION.get(meta_url, timeout=30)
        if r.status_code == 200:
            meta = r.json()
            cached = meta.get("cachedContents", {})
            avail = cached.get("rows", {}).get("availableCount")
            if avail is not None:
                return int(avail), "socrata_cached_contents"
    except Exception:
        pass

    # 2. $select=count(*) via resource API
    count_url = f"https://data.transportation.gov/resource/{dataset_id}.json?$select=count(*)"
    try:
        r = SESSION.get(count_url, timeout=45)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and data[0]:
                val = next(iter(data[0].values()), None)
                if val is not None:
                    return int(val), "socrata_count_query"
    except Exception:
        pass

    # 3. rows.csv HEAD — X-SODA2-Fields / X-SODA2-Count header (rare but possible)
    head_url = f"https://data.transportation.gov/api/views/{dataset_id}/rows.csv?accessType=DOWNLOAD"
    try:
        r = SESSION.head(head_url, timeout=30, allow_redirects=True)
        count_header = r.headers.get("X-SODA2-Count") or r.headers.get("x-soda2-count")
        if count_header:
            return int(count_header), "x_soda2_count_header"
    except Exception:
        pass

    return None, None


SAMPLE_BYTES = 524_288  # 512 KB


def sample_stream(url: str, max_bytes: int = SAMPLE_BYTES) -> bytes | None:
    """Download up to max_bytes from url via streaming GET, then close."""
    try:
        r = SESSION.get(url, timeout=60, stream=True)
        if not r.status_code == 200:
            r.close()
            return None
        chunks = []
        received = 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            remaining = max_bytes - received
            chunks.append(chunk[:remaining])
            received += len(chunk)
            if received >= max_bytes:
                break
        r.close()
        return b"".join(chunks)
    except Exception:
        return None


def estimate_row_count_from_sample(
    sample: bytes, file_size_bytes: int, has_header: bool
) -> tuple[int | None, str | None]:
    """Estimate total rows by measuring line density in the sample."""
    try:
        text = sample.decode("utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            return None, None
        # bytes consumed by sample lines (avoid partial last line)
        sample_text_bytes = len("\n".join(lines[:-1]).encode("utf-8"))
        data_lines = len(lines) - 1 - (1 if has_header else 0)
        if data_lines <= 0 or sample_text_bytes <= 0:
            return None, None
        bytes_per_row = sample_text_bytes / data_lines
        estimated_rows = int(round(file_size_bytes / bytes_per_row))
        return estimated_rows, "estimated_from_sample"
    except Exception:
        return None, None


def estimate_file_size_from_sample(
    sample: bytes, row_count: int, has_header: bool
) -> tuple[int | None, str | None]:
    """Estimate total file size from bytes-per-row in sample."""
    try:
        text = sample.decode("utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            return None, None
        data_lines = len(lines) - 1 - (1 if has_header else 0)
        if data_lines <= 0:
            return None, None
        # Use all but partial last line for byte measurement
        used_text = "\n".join(lines[:-1])
        bytes_per_row = len(used_text.encode("utf-8")) / data_lines
        # Add header row back
        header_bytes = len(lines[0].encode("utf-8")) + 1 if has_header else 0
        estimated_bytes = int(round(header_bytes + row_count * bytes_per_row))
        return estimated_bytes, "estimated_from_sample"
    except Exception:
        return None, None


def get_file_size_bytes(url: str) -> int | None:
    """HEAD request; follow redirects."""
    try:
        r = SESSION.head(url, timeout=30, allow_redirects=True)
        if r.status_code == 200:
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
    except Exception:
        pass

    # Some servers reject HEAD; try GET with stream and bail after headers
    try:
        r = SESSION.get(url, timeout=30, stream=True)
        if r.status_code == 200:
            cl = r.headers.get("Content-Length")
            r.close()
            if cl:
                return int(cl)
        else:
            try:
                r.close()
            except Exception:
                pass
    except Exception:
        pass

    return None


def measure_feed(feed: dict) -> dict:
    url = feed["download_url"]
    dataset_id = extract_dataset_id(url)
    print(f"  Measuring: {feed['feed_name']} (dataset={dataset_id})", file=sys.stderr)

    row_count, row_count_source = (None, None)
    file_size_bytes, file_size_source = (None, None)
    has_header = "/api/views/" in url  # CSV exports have a header row

    if dataset_id:
        row_count, row_count_source = get_row_count_from_metadata(dataset_id)

    # File size: try Content-Length from HEAD/GET first
    cl = get_file_size_bytes(url)
    if cl is not None:
        file_size_bytes = cl
        file_size_source = "content_length_header"

    # Both values present — done
    if file_size_bytes is not None and row_count is not None:
        pass
    # Missing row count (plain text feeds) — sample and extrapolate
    elif file_size_bytes is not None and row_count is None:
        sample = sample_stream(url)
        if sample:
            row_count, row_count_source = estimate_row_count_from_sample(
                sample, file_size_bytes, has_header
            )
    # Missing file size (CSV export feeds) — sample and extrapolate using known row count
    elif file_size_bytes is None and row_count is not None:
        sample = sample_stream(url)
        if sample:
            file_size_bytes, file_size_source = estimate_file_size_from_sample(
                sample, row_count, has_header
            )
    # Both missing — sample for what we can
    elif file_size_bytes is None and row_count is None:
        sample = sample_stream(url)
        # Can't extrapolate without anchor; just note the gap

    return {
        "feed_name": feed["feed_name"],
        "dataset_id": dataset_id,
        "download_url": url,
        "file_size_bytes": file_size_bytes,
        "file_size_mb": round(file_size_bytes / 1_048_576, 2) if file_size_bytes else None,
        "row_count": row_count,
        "row_count_source": row_count_source,
        "file_size_source": file_size_source,
    }


def main():
    print(f"Measuring {len(FEEDS)} FMCSA feeds...", file=sys.stderr)
    results = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(measure_feed, feed): feed for feed in FEEDS}
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                feed = futures[future]
                results.append({
                    "feed_name": feed["feed_name"],
                    "dataset_id": extract_dataset_id(feed["download_url"]),
                    "download_url": feed["download_url"],
                    "file_size_bytes": None,
                    "file_size_mb": None,
                    "row_count": None,
                    "row_count_source": None,
                    "file_size_source": None,
                    "error": str(e),
                })

    # Sort by file_size_bytes descending (nulls last)
    results.sort(key=lambda r: r["file_size_bytes"] or -1, reverse=True)

    output_path = "docs/fmcsa_feed_sizes.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nWrote {output_path}", file=sys.stderr)
    print(f"\n{'Feed Name':<40} {'Size MB':>10} {'Row Count':>12}", file=sys.stderr)
    print("-" * 65, file=sys.stderr)
    for r in results:
        size = f"{r['file_size_mb']}" if r["file_size_mb"] else "unknown"
        rows = f"{r['row_count']:,}" if r["row_count"] else "unknown"
        print(f"{r['feed_name']:<40} {size:>10} {rows:>12}", file=sys.stderr)


if __name__ == "__main__":
    main()
