#!/usr/bin/env python3
"""Epiq11 fresh-fetch → Cloudflare R2 (Pattern A transport layer).

Three subcommands match Epiq's three data surfaces:

  cases    → `POST /api/search/getcards type=Cases` → the universe (946 cases
             as of 2026-05-27). One parquet per run.
  claims   → `POST /api/search/getcards type=CasesClaims` per case → claims
             register. One parquet per case per run.
  dockets  → `POST /api/search/getcards type=CasesDockets` per case → docket
             register. One parquet per case per run.

The full API response row is preserved verbatim (URLs to PDFs, project home
pages, image references, document IDs all survive untouched as columns and
inside `raw_source_row`). PDF binaries are NOT downloaded — only the
references that point at them. Downstream consumers can fetch on demand
from `document.epiq11.com/document/getdocumentbycode?docId={...}`.

R2 layout (`s3://dex-raw-landing-zone/`):

    epiq/cases/run_date={YYYY-MM-DD}/{run_id}.parquet.zst
    epiq/claims/{project_code}/{run_id}.parquet.zst
    epiq/dockets/{project_code}/{run_id}.parquet.zst

Object content-encoding: parquet column-chunk ZSTD is INTERNAL to the file
(`pyarrow.parquet.write_table(..., compression="zstd")`). We do NOT set
`Content-Encoding: zstd` on the HTTP object — per
`~/Desktop/hq/inventory/DATA-FACTORY-LESSONS-LEARNED.md` §L42 that header
causes downstream readers (RW, sometimes DuckDB httpfs) to try to gunzip
the response body before parsing it as parquet, which mangles the bytes.

Doppler-injected env required:
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Usage:

    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_to_r2.py cases

    # Backfill claims + dockets for every case in the latest cases parquet:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_to_r2.py claims --all-cases
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_to_r2.py dockets --all-cases

    # Targeted re-pull:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_to_r2.py claims --cases spirit,silver-airways

    # Smoke (one case, one page):
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_to_r2.py claims --cases spirit --max-pages 1
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from uuid import UUID, uuid4

import boto3
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_CASES = "epiq/cases"
R2_PREFIX_CLAIMS = "epiq/claims"
R2_PREFIX_DOCKETS = "epiq/dockets"

EPIQ_API_URL = "https://dm.epiq11.com/api/search/getcards"
EPIQ_PAGE_SIZE = 500
# Epiq's API silently 500s past offset ~10,000 on any single (case, surface)
# query. Cap at 19 pages × 500 = 9500 rows; cases with more (e.g. acc at
# 71k) get truncated. Capturing the tail requires sliced queries (filedDate
# windows etc.) — separate scope.
EPIQ_MAX_PAGES_DEFAULT = 19
REQUEST_DELAY_SEC = 0.4         # polite-bot pacing

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# 403 is NOT retried — it's a legitimate "project not public" response from
# Epiq for restricted cases (~half the universe). Treated as skip-not-fail.
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ProjectNotPublic(Exception):
    """Marker raised when Epiq returns 403 ('project not public')."""

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("epiq-to-r2")


# --------------------------------------------------------------------------- #
# R2
# --------------------------------------------------------------------------- #


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _put_parquet(client, key: str, body: bytes) -> int:
    """Upload parquet bytes to R2 WITHOUT Content-Encoding (L42).

    Parquet ZSTD compression is internal to the file (column-chunk level);
    setting Content-Encoding: zstd on the HTTP object causes downstream
    readers to gunzip the body before parsing, breaking parquet bytes.
    """
    if not body:
        return 0
    client.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/x-parquet",
    )
    return len(body)


# --------------------------------------------------------------------------- #
# Epiq API
# --------------------------------------------------------------------------- #


def _request_body(card_type: str, *, case_slug: str | None, offset: int) -> dict:
    """Construct the POST body for one page of one surface.

    Contracts mirror dm.epiq11.com's SPA bundle — diverging in groupBy or
    searchType values produces a 500 from the endpoint.
    """
    if card_type == "Cases":
        return {
            "type": "Cases",
            "term": "",
            "groupBy": "caseName",
            "filters": [],
            "sort": "asc",
            "documentFrom": offset,
            "size": EPIQ_PAGE_SIZE,
            "origin": "Home",
            "userProjects": "",
        }
    if card_type == "CasesDockets":
        if not case_slug:
            raise ValueError("CasesDockets requires case_slug")
        return {
            "type": "CasesDockets",
            "term": "",
            "groupBy": "docketFiledDate",
            "filters": [
                {"name": "projectCode", "values": [case_slug]},
                {"name": "dbSource", "values": ["DM"]},
                {"name": "isAdversaryProceeding", "values": ["false"]},
            ],
            "sort": "desc",
            "documentFrom": offset,
            "size": EPIQ_PAGE_SIZE,
            "origin": "Case Page",
            "userProjects": "",
        }
    if card_type == "CasesClaims":
        if not case_slug:
            raise ValueError("CasesClaims requires case_slug")
        return {
            "type": "CasesClaims",
            "term": "",
            "groupBy": "claimNumber",
            "filters": [
                {"name": "projectCode", "values": [case_slug]},
                {"name": "searchType", "values": ["cs", "c", "s"]},
            ],
            "sort": "asc",
            "documentFrom": offset,
            "size": EPIQ_PAGE_SIZE,
            "origin": "Case Page",
            "userProjects": "",
        }
    raise ValueError(f"unknown card_type: {card_type}")


def _referer(card_type: str, case_slug: str | None) -> str:
    if card_type == "Cases":
        return "https://dm.epiq11.com/"
    if case_slug and card_type == "CasesDockets":
        return f"https://dm.epiq11.com/case/{case_slug}/dockets"
    if case_slug and card_type == "CasesClaims":
        return f"https://dm.epiq11.com/case/{case_slug}/claims"
    return "https://dm.epiq11.com/"


def _fetch_page(card_type: str, case_slug: str | None, offset: int) -> dict:
    body = _request_body(card_type, case_slug=case_slug, offset=offset)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": _referer(card_type, case_slug),
    }
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        time.sleep(REQUEST_DELAY_SEC)
        try:
            with httpx.Client(headers=headers, follow_redirects=True) as c:
                resp = c.post(EPIQ_API_URL, json=body, timeout=60.0)
            # 403 = "project not public" — terminal-skip, don't retry.
            if resp.status_code == 403:
                raise ProjectNotPublic(case_slug or "(no slug)")
            if resp.status_code in RETRY_STATUSES:
                resp.raise_for_status()
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
        except ProjectNotPublic:
            raise
        except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt >= 4:
                raise
            backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "epiq fetch card=%s case=%s offset=%d attempt %d failed (%s); sleeping %.2fs",
                card_type, case_slug, offset, attempt, exc, backoff,
            )
            time.sleep(backoff)
    raise last_exc  # type: ignore[misc]


def _paginate(card_type: str, case_slug: str | None, max_pages: int) -> tuple[list[dict], int | None]:
    """Walk pages until empty or max_pages hit. Returns (rows, upstream_total).

    Late 5xx errors (Epiq's deep-pagination ceiling at offset ~10,000) are
    swallowed if we've already accumulated rows — surface the truncated set
    as a successful partial pull rather than failing the entire case.
    """
    out: list[dict] = []
    total_upstream: int | None = None
    for page_num in range(max_pages):
        offset = page_num * EPIQ_PAGE_SIZE
        try:
            payload = _fetch_page(card_type, case_slug, offset)
        except httpx.HTTPStatusError as exc:
            # If we already have rows, treat the late 5xx as the upstream
            # pagination ceiling; truncate the pull rather than fail the case.
            if out and exc.response.status_code in (500, 502, 503, 504):
                log.warning(
                    "epiq paginate card=%s case=%s offset=%d hit %d after %d rows — "
                    "truncating (likely Epiq pagination ceiling)",
                    card_type, case_slug, offset, exc.response.status_code, len(out),
                )
                break
            raise
        if total_upstream is None:
            total_upstream = payload.get("total")

        page_rows: list[dict] = []
        for g in payload.get("groups", []) or []:
            for r in g.get("results", []) or []:
                page_rows.append(r)

        if not page_rows:
            break
        out.extend(page_rows)
        if len(page_rows) < EPIQ_PAGE_SIZE:
            break
    return out, total_upstream


# --------------------------------------------------------------------------- #
# Parquet
# --------------------------------------------------------------------------- #


def _rows_to_parquet_zstd(
    rows: list[dict],
    *,
    run_id: UUID,
    ingested_at: datetime,
    project_code: str | None = None,
    card_type: str,
) -> bytes:
    """Serialize rows to ZSTD parquet (column-chunk internal compression).

    Nested dicts/lists are JSON-stringified so pyarrow can infer a flat
    schema; the verbatim row is also preserved in `raw_source_row` as a
    JSON string for any field we don't promote to a top-level column. This
    keeps every URL reference, document ID, and metadata blob present at
    Lance time even if the typed projection doesn't enumerate it.
    """
    if not rows:
        return b""
    enriched: list[dict] = []
    run_id_str = str(run_id)
    for r in rows:
        flat: dict = {
            "source_run_id": run_id_str,
            "ingested_at": ingested_at,
            "card_type": card_type,
            "raw_source_row": json.dumps(r, default=str),
        }
        if project_code is not None:
            flat["project_code"] = project_code
        for k, v in r.items():
            if isinstance(v, (dict, list)):
                flat[k] = json.dumps(v, default=str)
            else:
                flat[k] = v
        enriched.append(flat)

    table = pa.Table.from_pylist(enriched)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=3)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Helpers — case-universe lookup from R2
# --------------------------------------------------------------------------- #


def _list_all_cases_from_r2(client) -> list[str]:
    """Return distinct project_code (canon) from the most-recent epiq/cases parquet.

    Falls back to a live API discovery call if no cases parquet exists yet
    (first run / cold-start condition).
    """
    paginator = client.get_paginator("list_objects_v2")
    latest_key: str | None = None
    latest_modified = None
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX_CASES}/"):
        for obj in page.get("Contents", []) or []:
            if not obj["Key"].endswith(".parquet.zst"):
                continue
            if latest_modified is None or obj["LastModified"] > latest_modified:
                latest_modified = obj["LastModified"]
                latest_key = obj["Key"]

    if latest_key:
        log.info("reading case list from R2 key=%s", latest_key)
        body = client.get_object(Bucket=R2_BUCKET, Key=latest_key)["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        # canon is the URL-slug column used as project_code in claims/dockets queries
        canons = table.column("canon").to_pylist() if "canon" in table.schema.names else []
        canons = [c for c in canons if c]
        log.info("loaded %d distinct case slugs from R2", len(set(canons)))
        return sorted(set(canons))

    log.info("no cases parquet in R2 — discovering live from API")
    rows, total = _paginate("Cases", None, EPIQ_MAX_PAGES_DEFAULT)
    canons = sorted({r.get("canon") for r in rows if r.get("canon")})
    log.info("discovered %d cases live (upstream_total=%s)", len(canons), total)
    return canons


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def cmd_cases(args: argparse.Namespace) -> int:
    client = _r2_client()
    run_id = uuid4()
    ingested_at = datetime.now(timezone.utc)
    run_date = ingested_at.date().isoformat()

    log.info("CASES START run_id=%s", run_id)
    rows, upstream_total = _paginate("Cases", None, args.max_pages)
    log.info("CASES fetched %d rows (upstream_total=%s)", len(rows), upstream_total)

    body = _rows_to_parquet_zstd(
        rows,
        run_id=run_id,
        ingested_at=ingested_at,
        project_code=None,
        card_type="Cases",
    )
    key = f"{R2_PREFIX_CASES}/run_date={run_date}/{run_id}.parquet.zst"
    n = _put_parquet(client, key, body)
    log.info("CASES wrote %d rows (%d bytes) → s3://%s/%s", len(rows), n, R2_BUCKET, key)
    print(json.dumps({
        "surface": "cases",
        "run_id": str(run_id),
        "rows": len(rows),
        "bytes": n,
        "upstream_total": upstream_total,
        "r2_key": key,
    }))
    return 0


def _ingest_one_case(
    client,
    card_type: str,
    r2_prefix: str,
    case_slug: str,
    run_id: UUID,
    ingested_at: datetime,
    max_pages: int,
) -> dict:
    rows, upstream_total = _paginate(card_type, case_slug, max_pages)
    body = _rows_to_parquet_zstd(
        rows,
        run_id=run_id,
        ingested_at=ingested_at,
        project_code=case_slug,
        card_type=card_type,
    )
    key = f"{r2_prefix}/{case_slug}/{run_id}.parquet.zst"
    n = _put_parquet(client, key, body)
    return {
        "case": case_slug,
        "rows": len(rows),
        "bytes": n,
        "upstream_total": upstream_total,
        "r2_key": key if n > 0 else None,
    }


def _ingest_many_cases(
    args: argparse.Namespace,
    card_type: str,
    r2_prefix: str,
    surface_label: str,
) -> int:
    client = _r2_client()
    run_id = uuid4()
    ingested_at = datetime.now(timezone.utc)

    if args.cases:
        cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    elif args.all_cases:
        cases = _list_all_cases_from_r2(client)
    else:
        log.error("%s: must pass either --cases <comma-list> or --all-cases", surface_label)
        return 2

    log.info("%s START run_id=%s cases=%d concurrency=%d",
             surface_label, run_id, len(cases), args.concurrency)
    summaries: list[dict] = []
    skipped: list[str] = []
    failures: list[dict] = []

    def _record(case_slug: str, fut_result: dict | None, exc: Exception | None) -> None:
        if exc is None and fut_result is not None:
            summaries.append(fut_result)
        elif isinstance(exc, ProjectNotPublic):
            skipped.append(case_slug)
        else:
            failures.append({"case": case_slug, "error": f"{type(exc).__name__}: {exc}"})

    if args.concurrency <= 1:
        for i, c in enumerate(cases, 1):
            try:
                s = _ingest_one_case(client, card_type, r2_prefix, c, run_id, ingested_at, args.max_pages)
                _record(c, s, None)
                log.info("%s [%d/%d] case=%s rows=%d upstream=%s",
                         surface_label, i, len(cases), c, s["rows"], s["upstream_total"])
            except ProjectNotPublic:
                _record(c, None, ProjectNotPublic(c))
                log.info("%s [%d/%d] case=%s SKIP (not public)", surface_label, i, len(cases), c)
            except Exception as exc:  # noqa: BLE001
                _record(c, None, exc)
                log.error("%s case=%s FAILED: %s", surface_label, c, exc)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = {
                ex.submit(_ingest_one_case, client, card_type, r2_prefix, c,
                          run_id, ingested_at, args.max_pages): c
                for c in cases
            }
            for i, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                try:
                    s = fut.result()
                    _record(c, s, None)
                    log.info("%s [%d/%d] case=%s rows=%d upstream=%s",
                             surface_label, i, len(cases), c, s["rows"], s["upstream_total"])
                except ProjectNotPublic:
                    _record(c, None, ProjectNotPublic(c))
                    log.info("%s [%d/%d] case=%s SKIP (not public)", surface_label, i, len(cases), c)
                except Exception as exc:  # noqa: BLE001
                    _record(c, None, exc)
                    log.error("%s case=%s FAILED: %s", surface_label, c, exc)

    total_rows = sum(s["rows"] for s in summaries)
    total_bytes = sum(s["bytes"] for s in summaries)
    log.info(
        "%s DONE cases_ok=%d cases_skipped=%d cases_failed=%d rows=%d bytes=%d",
        surface_label, len(summaries), len(skipped), len(failures), total_rows, total_bytes,
    )
    print(json.dumps({
        "surface": surface_label.lower(),
        "run_id": str(run_id),
        "cases_ok": len(summaries),
        "cases_skipped": len(skipped),
        "cases_failed": len(failures),
        "rows": total_rows,
        "bytes": total_bytes,
        "skipped_sample": skipped[:20],
        "failures": failures[:20],
    }))
    return 0 if not failures else 1


def cmd_claims(args: argparse.Namespace) -> int:
    return _ingest_many_cases(args, "CasesClaims", R2_PREFIX_CLAIMS, "CLAIMS")


def cmd_dockets(args: argparse.Namespace) -> int:
    return _ingest_many_cases(args, "CasesDockets", R2_PREFIX_DOCKETS, "DOCKETS")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_cases = sub.add_parser("cases", help="Pull Epiq case universe (type=Cases)")
    sp_cases.add_argument("--max-pages", type=int, default=EPIQ_MAX_PAGES_DEFAULT)
    sp_cases.set_defaults(func=cmd_cases)

    for name, cmd, doc in (
        ("claims", cmd_claims, "Pull claims register for one or many cases"),
        ("dockets", cmd_dockets, "Pull docket register for one or many cases"),
    ):
        sp = sub.add_parser(name, help=doc)
        grp = sp.add_mutually_exclusive_group(required=False)
        grp.add_argument("--cases", help="Comma-separated case slugs (canon column)")
        grp.add_argument("--all-cases", action="store_true",
                         help="Use the latest epiq/cases parquet as the case universe")
        sp.add_argument("--max-pages", type=int, default=EPIQ_MAX_PAGES_DEFAULT)
        sp.add_argument("--concurrency", type=int, default=1,
                        help="Parallel workers (each respects REQUEST_DELAY_SEC). Default 1.")
        sp.set_defaults(func=cmd)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
